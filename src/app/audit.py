"""The audit trail: who did what, whether it was allowed, and what the assistants cost.

Three kinds of row land in `audit_log`:

* RBAC changes (`role.grant`, `role.revoke`), written in the transaction that makes the change,
  so the trail and the roles cannot disagree;
* requests (`http.post` and so on): every request that changes something, and every request that
  was refused, written by the web app's middleware when the response is known;
* assistant activity (`agent.run`, `agent.tool_denied`), written by app.agents.runner.

Reading the trail is for system admins only. `detail` never holds an email address, a password
or free text from a form: rows describe an action, not the content of one.

`checks` are the deterministic findings (an organisation with no admin, a suspended account that
still holds a role, a burst of refusals). The audit assistant explains and orders them; it cannot
drop one (app.agents.audit).
"""

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Literal

from app import observability
from app.services.access import require_admin
from app.sqlstore import Database, days_ago, now

Outcome = Literal["ok", "denied", "invalid", "error"]
Source = Literal["web", "api", "agent", "system"]
Severity = Literal["high", "medium", "low"]

OUTCOMES: tuple[str, ...] = ("ok", "denied", "invalid", "error")
MAX_EVENTS = 500
# A person refused this many times in the window is worth a look.
DENIAL_BURST = 5
FAILED_SIGN_IN_BURST = 10


def outcome_for(status: int) -> Outcome:
    """The audit outcome of an HTTP status."""
    if status in (401, 403):
        return "denied"
    if status >= 500:
        return "error"
    if status >= 400:
        return "invalid"
    return "ok"


def record(
    conn: sqlite3.Connection,
    action: str,
    *,
    actor_id: str | None = None,
    outcome: Outcome = "ok",
    target_kind: str = "",
    target_id: str = "",
    org_id: str | None = None,
    source: Source = "system",
    detail: dict | None = None,
) -> None:
    """Add an event in the caller's transaction, so it commits with the change it describes."""
    conn.execute(
        "INSERT INTO audit_log (created_at, actor_id, action, outcome, target_kind, target_id, "
        "org_id, source, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (now(), actor_id, action, outcome, target_kind, target_id, org_id, source,
         json.dumps(detail or {}, sort_keys=True)),
    )  # fmt: skip
    observability.count_audit(action, outcome)


def record_now(db: Database, action: str, **fields) -> None:
    """Add an event in a transaction of its own (for events that follow no other write)."""
    with db.tx() as conn:
        record(conn, action, **fields)


# ---------------------------------------------------------------- reading (system admins)
def list_events(
    db: Database,
    actor_id: str,
    *,
    action: str = "",
    outcome: str = "",
    user_id: str = "",
    limit: int = 100,
) -> list[dict]:
    """The newest events first. `action` matches a prefix, so `role.` lists every role change."""
    require_admin(db, actor_id)
    if outcome and outcome not in OUTCOMES:
        outcome = ""
    with db.read() as conn:
        return _events(conn, action=action, outcome=outcome, user_id=user_id, limit=limit)


def _events(
    conn: sqlite3.Connection, *, action: str, outcome: str, user_id: str, limit: int
) -> list[dict]:
    rows = conn.execute(
        "SELECT a.id, a.created_at, a.actor_id, u.display_name AS actor, a.action, a.outcome, "
        "a.target_kind, a.target_id, a.org_id, o.name AS org, a.source, a.detail "
        "FROM audit_log a LEFT JOIN users u ON u.id = a.actor_id "
        "LEFT JOIN organisations o ON o.id = a.org_id "
        "WHERE (? = '' OR a.action LIKE ? ESCAPE '\\') AND (? = '' OR a.outcome = ?) "
        "AND (? = '' OR a.actor_id = ?) ORDER BY a.id DESC LIMIT ?",
        (action, _prefix(action), outcome, outcome, user_id, user_id,
         max(1, min(limit, MAX_EVENTS))),
    ).fetchall()  # fmt: skip
    return [dict(r) | {"detail": json.loads(r["detail"])} for r in rows]


def _prefix(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "%"


def rbac_snapshot(db: Database, actor_id: str) -> dict:
    """Who holds which role now: system roles, and per organisation its admins and reviewers.
    Display names and account status only; no contact details."""
    require_admin(db, actor_id)
    with db.read() as conn:
        return _rbac(conn)


def _rbac(conn: sqlite3.Connection) -> dict:
    system = [
        dict(r)
        for r in conn.execute(
            "SELECT r.user_id, u.display_name, u.status, r.role, r.granted_at, "
            "g.display_name AS granted_by FROM system_roles r JOIN users u ON u.id = r.user_id "
            "LEFT JOIN users g ON g.id = r.granted_by ORDER BY r.role, u.display_name, u.id"
        )
    ]
    holders: dict[str, list[dict]] = {}
    for r in conn.execute(
        "SELECT r.org_id, r.user_id, u.display_name, u.status, r.role, r.granted_at "
        "FROM org_roles r JOIN users u ON u.id = r.user_id ORDER BY r.role, u.display_name, u.id"
    ):
        holders.setdefault(r["org_id"], []).append(
            {k: r[k] for k in ("user_id", "display_name", "status", "role", "granted_at")}
        )
    organisations = [
        {
            "org_id": o["id"],
            "name": o["name"],
            "volunteers": o["volunteers"],
            "roles": holders.get(o["id"], []),
        }
        for o in conn.execute(
            "SELECT o.id, o.name, (SELECT count(*) FROM memberships m WHERE m.org_id = o.id "
            "AND m.opted_in = 1) AS volunteers FROM organisations o ORDER BY o.name"
        )
    ]
    return {"system_roles": system, "organisations": organisations}


def usage_summary(db: Database, actor_id: str, *, days: int = 7) -> dict:
    """What the application was used for in the last `days` days: events by action and outcome,
    the accounts refused most often, and what each assistant cost."""
    require_admin(db, actor_id)
    with db.read() as conn:
        return _usage(conn, days)


def _usage(conn: sqlite3.Connection, days: int) -> dict:
    days = max(1, min(days, 365))
    since = days_ago(days)
    actions = [
        dict(r)
        for r in conn.execute(
            "SELECT action, outcome, count(*) AS events FROM audit_log WHERE created_at >= ? "
            "GROUP BY action, outcome ORDER BY events DESC, action, outcome",
            (since,),
        )
    ]
    refused = [
        dict(r)
        for r in conn.execute(
            "SELECT a.actor_id, u.display_name, count(*) AS refusals FROM audit_log a "
            "JOIN users u ON u.id = a.actor_id WHERE a.created_at >= ? AND a.outcome = 'denied' "
            "GROUP BY a.actor_id ORDER BY refusals DESC, u.display_name LIMIT 10",
            (since,),
        )
    ]
    agents = [
        dict(r)
        for r in conn.execute(
            "SELECT agent, count(*) AS runs, count(DISTINCT actor_id) AS people, "
            "sum(status = 'completed') AS completed, sum(status = 'failed') AS failed, "
            "sum(status = 'awaiting_approval') AS awaiting_approval, "
            "sum(requests) AS requests, sum(input_tokens) AS input_tokens, "
            "sum(output_tokens) AS output_tokens, sum(tool_calls) AS tool_calls "
            "FROM agent_runs WHERE created_at >= ? GROUP BY agent ORDER BY agent",
            (since,),
        )
    ]
    totals = {o: 0 for o in OUTCOMES}
    for row in actions:
        totals[row["outcome"]] += row["events"]
    return {"days": days, "since": since, "outcomes": totals, "actions": actions,
            "most_refused": refused, "agents": agents}  # fmt: skip


# ---------------------------------------------------------------- deterministic checks
@dataclass(frozen=True)
class Check:
    """One finding the code can prove from the data. `id` is stable for the same subject, so a
    report can cite it and two reports can be compared."""

    id: str
    severity: Severity
    title: str
    evidence: str
    subjects: list[str] = field(default_factory=list)  # user or organisation IDs

    def as_dict(self) -> dict:
        return asdict(self)


def checks(db: Database, actor_id: str, *, days: int = 7) -> list[Check]:
    """Every deterministic finding, most severe first (system admins)."""
    require_admin(db, actor_id)
    return system_checks(db, days=days)


def system_checks(db: Database, *, days: int = 7) -> list[Check]:
    """The same findings with no acting user: for the operator's CLI and scheduled jobs only."""
    with db.read() as conn:
        found = _checks(conn, days)
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(found, key=lambda c: (order[c.severity], c.id))


def _checks(conn: sqlite3.Connection, days: int) -> list[Check]:
    since = days_ago(max(1, min(days, 365)))
    found: list[Check] = []
    admins = conn.execute(
        "SELECT r.user_id, u.display_name FROM system_roles r JOIN users u ON u.id = r.user_id "
        "WHERE r.role = 'admin' ORDER BY u.display_name"
    ).fetchall()
    if len(admins) == 1:
        found.append(
            Check(
                "single-system-admin", "medium", "Only one system admin",
                f"{admins[0]['display_name']} is the only account that can grant roles; if it is "
                "lost, nobody can.",
                [admins[0]["user_id"]],
            )
        )  # fmt: skip
    for r in conn.execute(
        "SELECT u.id, u.display_name, u.status, r.role, '' AS org FROM system_roles r "
        "JOIN users u ON u.id = r.user_id WHERE u.status IN ('suspended', 'declined') "
        "UNION ALL SELECT u.id, u.display_name, u.status, r.role, o.name FROM org_roles r "
        "JOIN users u ON u.id = r.user_id JOIN organisations o ON o.id = r.org_id "
        "WHERE u.status IN ('suspended', 'declined') ORDER BY 2, 4"
    ):
        where = f" for {r['org']}" if r["org"] else ""
        found.append(
            Check(
                f"role-held-while-{r['status']}:{r['id']}:{r['role']}", "high",
                f"A {r['status']} account still holds a role",
                f"{r['display_name']} is {r['status']} but is still {r['role']}{where}.",
                [r["id"]],
            )
        )  # fmt: skip
    for r in conn.execute(
        "SELECT o.id, o.name, (SELECT count(*) FROM submissions s WHERE s.org_id = o.id "
        "AND s.state = 'submitted') AS waiting FROM organisations o WHERE NOT EXISTS "
        "(SELECT 1 FROM org_roles r WHERE r.org_id = o.id AND r.role = 'org_admin') "
        "ORDER BY o.name"
    ):
        waiting = f" {r['waiting']} suggestion(s) are waiting for review." if r["waiting"] else ""
        found.append(
            Check(
                f"org-without-admin:{r['id']}", "high" if r["waiting"] else "medium",
                "An organisation has no admin",
                f"{r['name']} has no org admin, so only a system admin can manage it.{waiting}",
                [r["id"]],
            )
        )  # fmt: skip
    for r in conn.execute(
        "SELECT a.actor_id, u.display_name, count(*) AS n FROM audit_log a "
        "JOIN users u ON u.id = a.actor_id WHERE a.outcome = 'denied' AND a.created_at >= ? "
        "GROUP BY a.actor_id HAVING n >= ? ORDER BY n DESC",
        (since, DENIAL_BURST),
    ):
        found.append(
            Check(
                f"repeated-refusals:{r['actor_id']}", "medium", "An account was refused repeatedly",
                f"{r['display_name']} was refused {r['n']} times in the period: a missing role, "
                "or someone trying doors.",
                [r["actor_id"]],
            )
        )  # fmt: skip
    failed = conn.execute(
        "SELECT count(*) FROM audit_log WHERE action = 'auth.sign_in' AND outcome = 'denied' "
        "AND created_at >= ?",
        (since,),
    ).fetchone()[0]
    if failed >= FAILED_SIGN_IN_BURST:
        found.append(
            Check(
                "failed-sign-ins", "medium", "Many failed sign-ins",
                f"{failed} sign-in attempts failed in the period. There is no lock-out, so check "
                "whether they target one account.",
            )
        )  # fmt: skip
    for r in conn.execute(
        "SELECT a.actor_id, u.display_name, a.target_id, a.detail FROM audit_log a "
        "JOIN users u ON u.id = a.actor_id WHERE a.action = 'role.grant' AND a.outcome = 'ok' "
        "AND a.created_at >= ? AND a.actor_id = a.target_id ORDER BY a.id",
        (since,),
    ):
        role = json.loads(r["detail"]).get("role", "a role")
        found.append(
            Check(
                f"self-granted:{r['actor_id']}:{role}", "medium", "A role was self-granted",
                f"{r['display_name']} granted themselves {role}.",
                [r["actor_id"]],
            )
        )  # fmt: skip
    for r in conn.execute(
        "SELECT agent, count(*) AS n FROM agent_runs WHERE status = 'failed' AND created_at >= ? "
        "GROUP BY agent HAVING n >= 3 ORDER BY agent",
        (since,),
    ):
        found.append(
            Check(
                f"agent-failures:{r['agent']}", "low", "An assistant keeps failing",
                f"The {r['agent']} assistant failed {r['n']} times in the period.",
            )
        )  # fmt: skip
    return found
