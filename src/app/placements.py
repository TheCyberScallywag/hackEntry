"""Volunteer onboarding, step 2: from eligible to on the team.

1. After the graph changes, `sweep` asks Relate (MATCH_CANDIDATES, the same matching query as
   everywhere else) which active volunteers are eligible for which active projects. Each new pair
   becomes an `eligible` placement, and the volunteer and the project lead are notified in-app.
2. The volunteer and the project lead (or an org admin) each answer yes or no.
3. Two yeses put the volunteer on the team (`team`), projected to Relate as
   (:Volunteer)-[:ON_TEAM]->(:Project). A no on either side ends it (`declined`).
4. A pair that stops matching before both said yes (say, after a new exclusion) is `lapsed`;
   it becomes `eligible` again if it matches again. Team members are never removed by a sweep.

Every transition notifies the volunteer, so they always know where their application stands.
"""

import json
import logging
import sqlite3
from dataclasses import dataclass

from app import services
from app.catalogue import get_catalogue
from app.graph import cypher
from app.graph.store import GraphStore
from app.notify import notify
from app.sqlstore import Database, enqueue, now

log = logging.getLogger(__name__)

STATE_LABELS = {
    "eligible": "Eligible: awaiting answers",
    "team": "On the team",
    "declined": "Declined",
    "lapsed": "No longer eligible",
    "removed": "Removed",
    "left": "Left the team",
}


def _project_info(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    return conn.execute(
        "SELECT s.id, s.org_id, s.lead_id, s.state, s.title, o.name AS org_name "
        "FROM submission_current s JOIN organisations o ON o.id = s.org_id WHERE s.id = ?",
        (project_id,),
    ).fetchone()


@dataclass(frozen=True)
class Scope:
    """What a sweep has to look at: the volunteers and projects whose graph state just changed."""

    volunteers: frozenset[str] = frozenset()
    projects: frozenset[str] = frozenset()

    def __bool__(self) -> bool:
        return bool(self.volunteers or self.projects)


def _marks(ids: list[str]) -> str:
    return ",".join("?" * len(ids))


def _current_matches(
    db: Database, graph: GraphStore, scope: Scope | None
) -> dict[tuple[str, str], list[str]]:
    """Eligible (project, volunteer) pairs from Relate, each with the skills that matched.

    Every read is for one project or one volunteer, so the store's row bound applies to that one
    entity and not to an organisation as a whole. Hitting the bound raises GraphTruncated: the
    sweep fails and is retried, and no placement is lapsed on the strength of a partial answer.
    """
    with db.read() as conn:
        org_ids = [r[0] for r in conn.execute("SELECT id FROM organisations ORDER BY id")]
        if scope is None:
            projects = conn.execute(
                "SELECT id, org_id FROM submissions WHERE state = 'published' ORDER BY id"
            ).fetchall()
        else:
            ids = sorted(scope.projects)
            projects = conn.execute(
                f"SELECT id, org_id FROM submissions WHERE id IN ({_marks(ids)}) ORDER BY id", ids
            ).fetchall()
    reads = [cypher.match_params(orgs=[p["org_id"]], project=p["id"]) for p in projects]
    if scope is not None:
        reads += [cypher.match_params(orgs=org_ids, volunteer=v) for v in sorted(scope.volunteers)]
    current: dict[tuple[str, str], list[str]] = {}
    if not reads:
        return current
    with graph.reader() as tx:
        for params in reads:
            for row in tx.read(cypher.MATCH_CANDIDATES, params):
                current[(row["project"], row["volunteer"])] = sorted(row["matched"])
    return current


def _existing(conn: sqlite3.Connection, scope: Scope | None) -> dict[tuple[str, str], dict]:
    if scope is None:
        rows = conn.execute("SELECT * FROM placements")
    else:
        projects, volunteers = sorted(scope.projects), sorted(scope.volunteers)
        rows = conn.execute(
            f"SELECT * FROM placements WHERE project_id IN ({_marks(projects)}) "
            f"OR volunteer_id IN ({_marks(volunteers)})",
            (*projects, *volunteers),
        )
    return {(r["project_id"], r["volunteer_id"]): dict(r) for r in rows}


def sweep(db: Database, graph: GraphStore, scope: Scope | None = None) -> int:
    """Create, lapse or revive placements from the current Relate matches. Returns changes.

    With a `scope`, only pairs that involve one of its volunteers or projects are reconciled:
    the cost follows the size of the change, not the size of the system. Without one, every
    published project is reconciled (start-up, `cli drain`, an admin replay).
    """
    if scope is not None and not scope:
        return 0
    current = _current_matches(db, graph, scope)  # before the transaction: no lock held meanwhile
    changes = 0
    stamp = now()
    with db.tx() as conn:
        existing = _existing(conn, scope)
        live = {
            r["id"] for r in conn.execute("SELECT id FROM submissions WHERE state = 'published'")
        }
        active = {r["id"] for r in conn.execute("SELECT id FROM users WHERE status = 'active'")}
        for (pid, vid), skills in sorted(current.items()):
            if pid not in live or vid not in active:
                continue  # SQL freshness: only active projects and vetted volunteers
            before = existing.get((pid, vid))
            if before and before["state"] != "lapsed":
                continue
            info = _project_info(conn, pid)
            lead_yes = "yes" if info["lead_id"] == vid else None  # the lead is on their own team
            if before:
                conn.execute(
                    "UPDATE placements SET state = 'eligible', volunteer_said = NULL, "
                    "lead_said = ?, matched_skills = ?, version = version + 1, updated_at = ? "
                    "WHERE project_id = ? AND volunteer_id = ?",
                    (lead_yes, json.dumps(skills), stamp, pid, vid),
                )
            else:
                conn.execute(
                    "INSERT INTO placements (project_id, volunteer_id, state, lead_said, "
                    "matched_skills, created_at, updated_at) VALUES (?, ?, 'eligible', ?, ?, ?, ?)",
                    (pid, vid, lead_yes, json.dumps(skills), stamp, stamp),
                )
            notify(conn, vid, "placement",
                   f"You are eligible to volunteer on “{info['title']}” ({info['org_name']}). "
                   "Say yes to join the team.", "/opportunities")  # fmt: skip
            if info["lead_id"] and info["lead_id"] != vid:
                name = conn.execute(
                    "SELECT display_name FROM users WHERE id = ?", (vid,)
                ).fetchone()[0]
                notify(conn, info["lead_id"], "placement",
                       f"{name} is eligible for “{info['title']}”. Accept or decline them.",
                       f"/o/{info['org_id']}/projects/{pid}")  # fmt: skip
            changes += 1
        for (pid, vid), row in existing.items():
            if row["state"] == "eligible" and (pid, vid) not in current:
                conn.execute(
                    "UPDATE placements SET state = 'lapsed', version = version + 1, "
                    "updated_at = ? WHERE project_id = ? AND volunteer_id = ?",
                    (stamp, pid, vid),
                )
                info = _project_info(conn, pid)
                notify(conn, vid, "placement",
                       f"You are no longer eligible for “{info['title']}”, so that offer has "
                       "closed. Your profile or the project changed.",
                       "/opportunities")  # fmt: skip
                changes += 1
    return changes


def _respond(
    db: Database, project_id: str, volunteer_id: str, side: str, answer: str, version: int
) -> str:
    if answer not in ("yes", "no"):
        raise services.Invalid("Answer yes or no.")
    stamp = now()
    with db.tx() as conn:
        row = conn.execute(
            "SELECT * FROM placements WHERE project_id = ? AND volunteer_id = ?",
            (project_id, volunteer_id),
        ).fetchone()
        if row is None:
            raise services.NotFound("placement")
        if row["state"] != "eligible" or row["version"] != version:
            raise services.Conflict("This offer has changed since you opened it.")
        said = {"volunteer": row["volunteer_said"], "lead": row["lead_said"]} | {side: answer}
        if "no" in said.values():
            state = "declined"
        elif said["volunteer"] == "yes" and said["lead"] == "yes":
            state = "team"
        else:
            state = "eligible"
        conn.execute(
            "UPDATE placements SET volunteer_said = ?, lead_said = ?, state = ?, "
            "version = version + 1, updated_at = ? WHERE project_id = ? AND volunteer_id = ?",
            (said["volunteer"], said["lead"], state, stamp, project_id, volunteer_id),
        )
        info = _project_info(conn, project_id)
        name = conn.execute(
            "SELECT display_name FROM users WHERE id = ?", (volunteer_id,)
        ).fetchone()[0]
        title, lead = info["title"], info["lead_id"]
        project_link = f"/o/{info['org_id']}/projects/{project_id}"
        if state == "team":
            notify(conn, volunteer_id, "placement",
                   f"You are on the team for “{title}”.", "/opportunities")  # fmt: skip
            if lead and lead != volunteer_id:
                notify(conn, lead, "placement", f"{name} joined the team for “{title}”.",
                       project_link)  # fmt: skip
            enqueue(conn, "project", project_id)  # adds the ON_TEAM edge in Relate
        elif state == "declined":
            if side == "lead":
                notify(conn, volunteer_id, "placement",
                       f"The project lead for “{title}” has not taken your place this time.",
                       "/opportunities")  # fmt: skip
            elif lead and lead != volunteer_id:
                notify(conn, lead, "placement", f"{name} declined “{title}”.", project_link)
        elif side == "lead":
            notify(conn, volunteer_id, "placement",
                   f"The project lead for “{title}” would like you on the team. Say yes to join.",
                   "/opportunities")  # fmt: skip
        else:
            notify(conn, volunteer_id, "placement",
                   f"You said yes to “{title}”. Waiting for the project lead.",
                   "/opportunities")  # fmt: skip
            if lead and lead != volunteer_id:
                notify(conn, lead, "placement", f"{name} wants to join “{title}”.", project_link)
    return state


def volunteer_respond(
    db: Database, user_id: str, project_id: str, answer: str, *, version: int
) -> str:
    """The volunteer answers for themselves (their own placement only)."""
    services.require_active(db, user_id)
    return _respond(db, project_id, user_id, "volunteer", answer, version)


def _check_lead(conn: sqlite3.Connection, actor_id: str, project_id: str) -> sqlite3.Row:
    info = _project_info(conn, project_id)
    if info is None:
        raise services.NotFound("project")
    access = services.org_access(conn, actor_id, info["org_id"])
    if not (info["lead_id"] == actor_id or access.org_admin):
        if access.any:
            raise services.Forbidden("Only the project lead or an org admin can do that.")
        raise services.NotFound("project")
    return info


def _require_lead(db: Database, actor_id: str, project_id: str) -> sqlite3.Row:
    with db.read() as conn:
        return _check_lead(conn, actor_id, project_id)


def lead_respond(
    db: Database, actor_id: str, project_id: str, volunteer_id: str, answer: str, *, version: int
) -> str:
    """The project lead, an org admin or a system admin answers for the project."""
    _require_lead(db, actor_id, project_id)
    return _respond(db, project_id, volunteer_id, "lead", answer, version)


def end_team_membership(
    db: Database, actor_id: str, project_id: str, volunteer_id: str, *, reason: str = ""
) -> None:
    """The volunteer leaves, or the lead or an org admin removes them from the team."""
    leaving = actor_id == volunteer_id
    if not leaving:
        _require_lead(db, actor_id, project_id)
        reason = services.require_reason(reason)
    with db.tx() as conn:
        updated = conn.execute(
            "UPDATE placements SET state = ?, version = version + 1, updated_at = ? "
            "WHERE project_id = ? AND volunteer_id = ? AND state = 'team'",
            ("left" if leaving else "removed", now(), project_id, volunteer_id),
        ).rowcount
        if not updated:
            raise services.NotFound("team member")
        info = _project_info(conn, project_id)
        if leaving:
            notify(conn, volunteer_id, "placement", f"You left the team for “{info['title']}”.",
                   "/opportunities")  # fmt: skip
            if info["lead_id"] and info["lead_id"] != volunteer_id:
                notify(conn, info["lead_id"], "placement",
                       f"A volunteer left the team for “{info['title']}”.",
                       f"/o/{info['org_id']}/projects/{project_id}")  # fmt: skip
        else:
            notify(conn, volunteer_id, "placement",
                   f"You were removed from the team for “{info['title']}”: {reason}",
                   "/opportunities")  # fmt: skip
        enqueue(conn, "project", project_id)


def for_volunteer(db: Database, user_id: str) -> list[dict]:
    with db.read() as conn:
        rows = conn.execute(
            "SELECT p.*, s.title, o.name AS org_name, s.org_id FROM placements p "
            "JOIN submission_current s ON s.id = p.project_id "
            "JOIN organisations o ON o.id = s.org_id WHERE p.volunteer_id = ? "
            "ORDER BY CASE p.state WHEN 'eligible' THEN 0 WHEN 'team' THEN 1 ELSE 2 END, "
            "p.updated_at DESC",
            (user_id,),
        ).fetchall()
    return [_shape(r) for r in rows]


def for_project(db: Database, actor_id: str, project_id: str) -> list[dict]:
    """Candidates and team for the project lead and org admins (display names, no emails)."""
    with db.read() as conn:
        _check_lead(conn, actor_id, project_id)
        rows = conn.execute(
            "SELECT p.*, u.display_name FROM placements p JOIN users u ON u.id = p.volunteer_id "
            "WHERE p.project_id = ? ORDER BY CASE p.state WHEN 'eligible' THEN 0 "
            "WHEN 'team' THEN 1 ELSE 2 END, u.display_name",
            (project_id,),
        ).fetchall()
    return [_shape(r) for r in rows]


def _shape(row: sqlite3.Row) -> dict:
    data = dict(row)
    names = get_catalogue()
    data["matched_skills"] = [names.skill_name(s) for s in json.loads(row["matched_skills"])]
    data["state_label"] = STATE_LABELS[row["state"]]
    return data
