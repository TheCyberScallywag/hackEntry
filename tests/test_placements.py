"""The placement sweep: complete whatever the row bound, and scoped to what changed."""

import pytest

from app import placements, sqlstore
from app.config import Settings
from app.graph.projector import Changed
from app.graph.store import GraphStore, GraphTruncated
from tests.conftest import AUDIT, DK, FOOD_BANK, USERS, WINTER, Web
from tests.test_matching import save_profile
from tests.test_submissions import _review


def _states(web: Web) -> dict[tuple[str, str], str]:
    with web.rt.db.read() as conn:
        return {
            (r["project_id"], r["volunteer_id"]): r["state"]
            for r in conn.execute("SELECT project_id, volunteer_id, state FROM placements")
        }


def _forget(web: Web, project: str, volunteer: str) -> None:
    with web.rt.db.tx() as conn:
        conn.execute(
            "DELETE FROM placements WHERE project_id = ? AND volunteer_id = ?",
            (project, volunteer),
        )


@pytest.fixture
def settings(tmp_path) -> Settings:
    """A row bound of 4: enough for any one demo project, too few for a whole organisation
    once DataKind UK has two published projects."""
    return Settings(
        data_dir=tmp_path / "var", projector_interval_seconds=3600, graph_max_rows=4,
        _env_file=None,
    )  # fmt: skip


def test_no_offer_lapses_when_an_organisation_has_more_pairs_than_the_row_bound(web: Web):
    before = _states(web)
    assert sum(1 for (p, _), s in before.items() if p == FOOD_BANK and s == "eligible") == 3
    assert before[(FOOD_BANK, USERS["cerys"])] == "team"  # its lead: four pairs in the graph
    web.sign_in("rhiannon")
    assert _review(web, DK, AUDIT, "approved").status_code == 303  # a second DataKind UK project
    after = _states(web)
    audit_pairs = [pair for pair in after if pair[0] == AUDIT]
    assert audit_pairs, "the newly published project found its candidates"
    assert len(audit_pairs) + 4 > web.rt.graph.max_rows  # the organisation is over the bound
    assert {pair: after[pair] for pair in before} == before  # and nothing was lapsed
    assert placements.sweep(web.rt.db, web.rt.graph) == 0  # a full sweep agrees
    assert "lapsed" not in _states(web).values()


def test_a_read_over_the_bound_fails_loudly_and_changes_nothing(web: Web):
    before = _states(web)
    too_small = GraphStore(web.rt.graph.path, max_rows=3)  # the food bank project has 4 pairs
    with pytest.raises(GraphTruncated):
        placements.sweep(web.rt.db, too_small)
    assert _states(web) == before


def test_a_failed_sweep_is_retried_as_a_full_sweep_on_the_next_drain(web: Web):
    calls: list[Changed | None] = []
    real = web.rt.projector.on_projected

    def flaky(changed):
        calls.append(changed)
        if len(calls) == 1:
            raise GraphTruncated("simulated")
        return real(changed)

    web.rt.projector.on_projected = flaky
    save_profile(web, "dev", skills={"first-aid": "has"})
    assert isinstance(calls[0], Changed)
    web.rt.projector.drain()  # nothing queued, but the hook is still owed
    assert calls[1] is None


def test_a_profile_save_sweeps_only_that_volunteer(web: Web):
    seen: list[Changed | None] = []
    real = web.rt.projector.on_projected
    web.rt.projector.on_projected = lambda changed: (seen.append(changed), real(changed))
    _forget(web, FOOD_BANK, USERS["asha"])  # an unrelated pair that a full sweep would recreate
    save_profile(web, "dev", skills={"first-aid": "has"})
    assert len(seen) == 1 and seen[0] is not None
    assert seen[0].volunteers == {USERS["dev"]} and not seen[0].projects
    assert not seen[0].everything
    assert (FOOD_BANK, USERS["asha"]) not in _states(web)  # out of scope: left alone
    assert placements.sweep(web.rt.db, web.rt.graph) == 1  # the full sweep picks it up
    assert _states(web)[(FOOD_BANK, USERS["asha"])] == "eligible"


def test_scoped_sweep_reconciles_a_volunteer_or_a_project(web: Web):
    db, graph = web.rt.db, web.rt.graph
    _forget(web, FOOD_BANK, USERS["dev"])
    _forget(web, FOOD_BANK, USERS["kofi"])
    _forget(web, WINTER, USERS["asha"])
    assert placements.sweep(db, graph, placements.Scope()) == 0  # nothing in scope: no work
    only_dev = placements.Scope(volunteers=frozenset({USERS["dev"]}))
    assert placements.sweep(db, graph, only_dev) == 1
    assert (FOOD_BANK, USERS["dev"]) in _states(web)
    assert (FOOD_BANK, USERS["kofi"]) not in _states(web)
    only_food_bank = placements.Scope(projects=frozenset({FOOD_BANK}))
    assert placements.sweep(db, graph, only_food_bank) == 1
    assert (FOOD_BANK, USERS["kofi"]) in _states(web)
    assert (WINTER, USERS["asha"]) not in _states(web)
    assert placements.sweep(db, graph) == 1  # the rest


def test_scoped_sweep_lapses_and_revives_like_the_full_one(web: Web):
    save_profile(web, "dev", skills={"data-analysis": "exclude"})
    lapsed = {pair for pair, state in _states(web).items() if state == "lapsed"}
    assert lapsed == {(FOOD_BANK, USERS["dev"])}  # only Dev's own offer closed
    save_profile(web, "dev", skills={"data-analysis": "offers"})
    assert _states(web)[(FOOD_BANK, USERS["dev"])] == "eligible"


def test_a_replay_sweeps_everything(web: Web):
    seen: list[Changed | None] = []
    web.rt.projector.on_projected = seen.append
    with web.rt.db.tx() as conn:
        conn.execute(
            "INSERT INTO outbox (kind, entity_id, created_at, next_attempt_at) "
            "VALUES ('volunteer', ?, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
            (USERS["dev"],),
        )
    web.rt.projector.drain(ignore_backoff=True)
    assert seen == [None]


def test_an_organisation_change_sweeps_everything(web: Web):
    changed = Changed()
    changed.add("volunteer", "u_1")
    changed.add("project", "p_1")
    assert (changed.volunteers, changed.projects, changed.everything) == ({"u_1"}, {"p_1"}, False)
    changed.add("organisation", "org_1")
    assert changed.everything


def test_a_restart_sweeps_even_when_nothing_is_queued(web: Web, settings):
    """A sweep that failed is only remembered in memory. After a restart the outbox is empty, so
    the first drain of the new process must look at everything of its own accord."""
    from app.runtime import build_runtime

    restarted = build_runtime(settings)  # the same data directory, a new process's state
    seen = []
    restarted.projector.on_projected = seen.append
    assert restarted.projector.drain().done == 0
    assert seen == [None]  # a full sweep
    restarted.projector.drain()
    assert seen == [None]  # and then only when something changes


def test_a_sweep_that_fails_unexpectedly_is_still_owed(web: Web):
    real = web.rt.projector.on_projected

    def broken(_changed):
        raise KeyError("a bug in the sweep")

    web.rt.projector.on_projected = broken
    with web.rt.db.tx() as conn:
        sqlstore.enqueue(conn, "volunteer", USERS["dev"])
    with pytest.raises(KeyError):
        web.rt.projector.drain()
    seen = []
    web.rt.projector.on_projected = lambda changed: (seen.append(changed), real(changed))
    web.rt.projector.drain()  # the item is done; the sweep is not
    assert seen == [None]


def test_a_scoped_sweep_asks_sqlite_only_about_its_own_pairs(web: Web, monkeypatch):
    statements: list[str] = []
    real_connect = sqlstore.connect

    def connect(path):
        conn = real_connect(path)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(sqlstore, "connect", connect)
    placements.sweep(web.rt.db, web.rt.graph, placements.Scope(frozenset({USERS["dev"]})))

    def freshness() -> list[str]:
        asked = ("SELECT id, lead_id FROM submissions", "SELECT id FROM users WHERE status")
        return [s for s in statements if s.startswith(asked)]

    assert len(freshness()) == 2 and all(" AND id IN (" in s for s in freshness())
    statements.clear()
    placements.sweep(web.rt.db, web.rt.graph)
    assert len(freshness()) == 2 and not any(" AND id IN (" in s for s in freshness())
