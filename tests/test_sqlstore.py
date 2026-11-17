"""SQLite housekeeping: cheap connections, the indexes the queries rely on, and pruning."""

import sqlite3

from app import sqlstore
from app.sqlstore import connect, days_ago, now, prune
from tests.conftest import DK, FOOD_BANK, USERS, Web


def test_wal_is_set_once_on_the_file_and_every_connection_enforces_foreign_keys(web: Web):
    conn = connect(web.rt.settings.sqlite_path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"  # persisted by init
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 10_000  # from timeout=10
    finally:
        conn.close()


def test_opening_a_connection_runs_a_single_statement(web: Web, monkeypatch):
    statements: list[str] = []

    class Traced(sqlite3.Connection):
        def execute(self, sql, *args):
            statements.append(sql)
            return super().execute(sql, *args)

    real = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda path, **kw: real(path, factory=Traced, **kw))
    connect(web.rt.settings.sqlite_path).close()
    assert statements == ["PRAGMA foreign_keys = ON"]


def test_lookup_indexes_exist_and_are_used(web: Web):
    with web.rt.db.read() as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        assert {
            "placements_volunteer", "memberships_org", "submissions_author", "reviews_submission",
            "sessions_user", "org_roles_org", "vetting_events_user", "outbox_due",
        } <= names  # fmt: skip
        assert "outbox_pending" not in names  # replaced by the partial index

        def plan(sql: str, *params) -> str:
            return " ".join(r[3] for r in conn.execute(f"EXPLAIN QUERY PLAN {sql}", params))

        assert "placements_volunteer" in plan(
            "SELECT * FROM placements WHERE volunteer_id = ? AND state = 'team'", USERS["dev"]
        )
        assert "memberships_org" in plan(
            "SELECT user_id FROM memberships WHERE org_id = ? AND opted_in = 1", DK
        )
        assert "outbox_due" in plan(
            "SELECT 1 FROM outbox WHERE done_at IS NULL AND kind = 'project' AND entity_id = ?",
            FOOD_BANK,
        )
        assert "reviews_submission" in plan(
            "SELECT * FROM reviews WHERE submission_id = ?", FOOD_BANK
        )


def test_submission_current_view_carries_the_current_title(web: Web):
    with web.rt.db.read() as conn:
        row = conn.execute(
            "SELECT title, state, org_id FROM submission_current WHERE id = ?", (FOOD_BANK,)
        ).fetchone()
    assert tuple(row) == ("Food Bank Demand Dashboard", "published", DK)


def test_prune_deletes_only_what_nothing_reads_any_more(web: Web):
    old, recent, dev = days_ago(400), now(), USERS["dev"]
    with web.rt.db.tx() as conn:
        outbox_before = conn.execute("SELECT count(*) FROM outbox").fetchone()[0]
        conn.execute(
            "INSERT INTO outbox (kind, entity_id, created_at, next_attempt_at, done_at) "
            "VALUES ('volunteer', ?, ?, ?, ?)",
            (dev, old, old, old),
        )
        conn.execute(
            "INSERT INTO outbox (kind, entity_id, created_at, next_attempt_at) "
            "VALUES ('volunteer', 'old-but-still-pending', ?, ?)",
            (old, old),
        )
        for token, created, expires in (
            ("older", days_ago(500), days_ago(499)),  # expired long ago, superseded: deleted
            ("newest-expired", old, days_ago(399)),  # expired, but the user's newest: kept
        ):
            conn.execute(
                "INSERT INTO sessions (token_hash, user_id, kind, csrf_token, created_at, "
                "expires_at) VALUES (?, ?, 'cookie', 'c', ?, ?)",
                (token, dev, created, expires),
            )
        conn.execute("DELETE FROM notifications WHERE user_id = ?", (dev,))
        for message, read_at in (("read long ago", old), ("read today", recent), ("unread", None)):
            conn.execute(
                "INSERT INTO notifications (user_id, kind, message, created_at, read_at) "
                "VALUES (?, 'test', ?, ?, ?)",
                (dev, message, old, read_at),
            )
        assert prune(conn) == 3
        assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == outbox_before + 1
        assert conn.execute(
            "SELECT 1 FROM outbox WHERE entity_id = 'old-but-still-pending'"
        ).fetchone()
        tokens = [r[0] for r in conn.execute("SELECT token_hash FROM sessions WHERE user_id = ?",
                                              (dev,))]  # fmt: skip
        assert tokens == ["newest-expired"]
        kept = [r[0] for r in conn.execute(
            "SELECT message FROM notifications WHERE user_id = ? ORDER BY id", (dev,))]  # fmt: skip
        assert kept == ["read today", "unread"]
        assert prune(conn) == 0  # idempotent
        conn.execute("DELETE FROM outbox WHERE entity_id = 'old-but-still-pending'")


def test_retention_periods_are_declared():
    assert sqlstore.OUTBOX_DONE_DAYS == 7 and sqlstore.SESSION_ENDED_DAYS == 7
    assert sqlstore.NOTIFICATION_READ_DAYS == 90
