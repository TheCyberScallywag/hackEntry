"""Participation history: what a volunteer has done, and how it went.

An *engagement* is one spell on a project's team. It is opened and closed from the placement
(`sync`), never by hand, so the history cannot disagree with who was on the team. When an
engagement has ended, the project's lead side may rate it on one four-step scale (`LEVELS`):
overall readiness, and a level for each skill they saw used. A volunteer uses the same scale
for the level they give their own skills on their profile; the two are kept apart, because a
self-declared level and a lead's rating are different kinds of evidence.

Rules that protect the volunteer:

* they see every rating about them, who gave it, and the note written for them;
* nobody rates their own engagement;
* the lead's private note stays in SQLite and never reaches the graph;
* another organisation sees a summary of it (counts and the best rating, never which
  organisation or project) only if the volunteer switched sharing on for that organisation
  (`set_share_history`). In the graph that consent is an edge, SHARES_HISTORY_WITH, so a query
  from an organisation without it cannot reach the history at all.
"""

import hashlib
import sqlite3

from app.catalogue import get_catalogue
from app.notify import notify
from app.services import teams
from app.services.access import org_access, require_access
from app.services.errors import NOTE_MAX, Conflict, Forbidden, Invalid, NotFound
from app.sqlstore import Database, enqueue, now

# Level -> label: the one scale, for a skill and for an engagement as a whole.
LEVELS = {
    1: "Needs support",
    2: "Works independently",
    3: "Can guide others",
    4: "Could lead a project",
}
OUTCOMES = {
    "completed": "Project completed",
    "left": "Left the team",
    "removed": "Removed from the team",
    "withdrawn": "Project withdrawn",
}
ROLES = {"contributor": "Team member", "lead": "Project lead"}
# Placement states in which the volunteer is still part of the team's story.
_OPEN_STATES = ("team", "suspended")


def engagement_id(project_id: str, volunteer_id: str, spell: int) -> str:
    """An opaque ID that depends only on whose spell on which project it is, and which one:
    the same history always gets the same IDs, so a rebuilt graph equals the one before."""
    digest = hashlib.sha256(f"{project_id}:{volunteer_id}:{spell}".encode()).hexdigest()
    return f"e_{digest[:16]}"


def sync(conn: sqlite3.Connection, project_id: str) -> int:
    """Make the project's engagements agree with its placements: open one for each volunteer
    on the team who has none open, and close those whose place has ended. Returns how many
    changed. Runs whenever the project is projected, which every team change triggers."""
    sub = conn.execute(
        "SELECT state, lead_id FROM submissions WHERE id = ?", (project_id,)
    ).fetchone()
    if sub is None:
        return 0
    live = sub["state"] in ("approved", "published")
    places = {
        r["volunteer_id"]: r["state"]
        for r in conn.execute(
            "SELECT volunteer_id, state FROM placements WHERE project_id = ?", (project_id,)
        )
    }
    open_now = {
        r["volunteer_id"]: r["id"]
        for r in conn.execute(
            "SELECT id, volunteer_id FROM engagements WHERE project_id = ? AND ended_at IS NULL",
            (project_id,),
        )
    }
    stamp, changed = now(), 0
    for volunteer_id, open_id in sorted(open_now.items()):
        state = places.get(volunteer_id)
        if live and state in _OPEN_STATES:
            continue
        if sub["state"] == "withdrawn":
            outcome = "withdrawn"
        elif state == "completed" or sub["state"] == "completed":
            outcome = "completed"
        else:
            outcome = "left" if state == "left" else "removed"
        conn.execute(
            "UPDATE engagements SET ended_at = ?, outcome = ? WHERE id = ?",
            (stamp, outcome, open_id),
        )
        changed += 1
    if live:
        for volunteer_id, state in sorted(places.items()):
            if state == "team" and volunteer_id not in open_now:
                earlier = conn.execute(
                    "SELECT count(*) FROM engagements WHERE project_id = ? AND volunteer_id = ?",
                    (project_id, volunteer_id),
                ).fetchone()[0]
                conn.execute(
                    "INSERT INTO engagements (id, project_id, volunteer_id, role, started_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (engagement_id(project_id, volunteer_id, earlier), project_id, volunteer_id,
                     "lead" if volunteer_id == sub["lead_id"] else "contributor", stamp),
                )  # fmt: skip
                changed += 1
    return changed


def complete_project(
    db: Database, actor_id: str, org_id: str, project_id: str, *, expected_version: int
) -> None:
    """The lead side marks a published project as done. It stops matching, its team's
    engagements end as completed, and it stays in the graph as history."""
    stamp = now()
    with db.tx() as conn:
        info = teams.check_lead_side(conn, actor_id, project_id)
        if info["org_id"] != org_id:
            raise NotFound("project")
        updated = conn.execute(
            "UPDATE submissions SET state = 'completed', version = version + 1, "
            "completed_by = ?, completed_at = ?, updated_at = ? "
            "WHERE id = ? AND state = 'published' AND version = ?",
            (actor_id, stamp, stamp, project_id, expected_version),
        ).rowcount
        if not updated:
            raise Conflict("This project is not active any more, or it changed.")
        affected = conn.execute(
            "SELECT volunteer_id, state, lead_said FROM placements WHERE project_id = ? "
            "AND state IN ('eligible', 'onboarding', 'team', 'suspended')",
            (project_id,),
        ).fetchall()
        for row in affected:
            on_team = row["state"] == "team"
            conn.execute(
                "UPDATE placements SET state = ?, version = version + 1, updated_at = ? "
                "WHERE project_id = ? AND volunteer_id = ?",
                ("completed" if on_team else "lapsed", stamp, project_id, row["volunteer_id"]),
            )
            teams.record(conn, project_id, row["volunteer_id"], "project_completed",
                         actor_id=actor_id)  # fmt: skip
            # Candidates who were never invited were never told, so they are not told now.
            if row["volunteer_id"] != actor_id and (
                row["state"] != "eligible" or row["lead_said"] == "yes"
            ):
                notify(
                    conn, row["volunteer_id"], "placement",
                    f"“{info['title']}” is complete. " + (
                        "Thank you: it is now part of your history." if on_team
                        else "Your invitation to it has closed."),
                    "/profile#history" if on_team else "/opportunities",
                )  # fmt: skip
        conn.execute(
            "UPDATE onboarding_runs SET state = 'cancelled' WHERE project_id = ? "
            "AND state != 'cancelled'",
            (project_id,),
        )
        enqueue(conn, "project", project_id)


def _levels(values: dict[str, int], allowed: set[str]) -> dict[str, int]:
    if set(values) - allowed:
        raise Invalid("A skill that is not one of the project's skills cannot be rated.")
    if any(level not in LEVELS for level in values.values()):
        raise Invalid("A level must be between 1 and 4.")
    return values


def rate(
    db: Database,
    actor_id: str,
    engagement_id: str,
    *,
    readiness: int,
    skills: dict[str, int] | None = None,
    note: str = "",
    private_note: str = "",
) -> None:
    """The project's lead side says how an ended engagement went. Rating again replaces the
    earlier rating. The volunteer is told, and can read it on their profile."""
    if readiness not in LEVELS:
        raise Invalid("Readiness must be between 1 and 4.")
    note, private_note = note.strip(), private_note.strip()
    if len(note) > NOTE_MAX or len(private_note) > NOTE_MAX:
        raise Invalid(f"A note can be at most {NOTE_MAX} characters.")
    with db.tx() as conn:
        row = conn.execute(
            "SELECT project_id, volunteer_id, ended_at FROM engagements WHERE id = ?",
            (engagement_id,),
        ).fetchone()
        if row is None:
            raise NotFound("engagement")
        info = teams.check_lead_side(conn, actor_id, row["project_id"])
        if row["volunteer_id"] == actor_id:
            raise Forbidden("Someone else must rate your own time on a project.")
        if row["ended_at"] is None:
            raise Conflict("This volunteer is still on the team. Rate them when it has ended.")
        allowed = {
            r[0]
            for r in conn.execute(
                "SELECT k.skill_id FROM revision_skills k JOIN submissions s "
                "ON s.id = k.submission_id AND s.approved_revision = k.revision WHERE s.id = ?",
                (row["project_id"],),
            )
        }
        levels = _levels(skills or {}, allowed)
        conn.execute(
            "UPDATE engagements SET readiness = ?, rated_by = ?, rated_at = ?, "
            "note_for_volunteer = ?, private_note = ? WHERE id = ?",
            (readiness, actor_id, now(), note, private_note, engagement_id),
        )
        conn.execute("DELETE FROM engagement_skills WHERE engagement_id = ?", (engagement_id,))
        conn.executemany(
            "INSERT INTO engagement_skills (engagement_id, skill_id, level) VALUES (?, ?, ?)",
            [(engagement_id, skill, level) for skill, level in sorted(levels.items())],
        )
        notify(conn, row["volunteer_id"], "placement",
               f"Your time on “{info['title']}” has been reviewed: {LEVELS[readiness]}.",
               "/profile#history")  # fmt: skip
        enqueue(conn, "project", row["project_id"])


def _shape(row: sqlite3.Row, skills: dict[str, list[dict]]) -> dict:
    return {
        **dict(row),
        "role_label": ROLES[row["role"]],
        "outcome_label": OUTCOMES[row["outcome"]] if row["outcome"] else "On the team now",
        "readiness_label": LEVELS[row["readiness"]] if row["readiness"] else None,
        "skills": skills.get(row["id"], []),
    }


def _skills(conn: sqlite3.Connection, where: str, params: tuple) -> dict[str, list[dict]]:
    cat, found = get_catalogue(), {}
    for r in conn.execute(
        "SELECT k.engagement_id, k.skill_id, k.level FROM engagement_skills k "
        f"JOIN engagements e ON e.id = k.engagement_id WHERE {where} ORDER BY k.skill_id",  # noqa: S608
        params,
    ):
        found.setdefault(r["engagement_id"], []).append(
            {"id": r["skill_id"], "name": cat.skill_name(r["skill_id"]), "level": r["level"],
             "level_label": LEVELS[r["level"]]}
        )  # fmt: skip
    return found


_COLUMNS = (
    "e.id, e.project_id, e.volunteer_id, e.role, e.started_at, e.ended_at, e.outcome, "
    "e.readiness, e.rated_at, e.note_for_volunteer, s.title, s.org_id, o.name AS org_name, "
    "r.display_name AS rated_by_name"
)
_JOINS = (
    "FROM engagements e JOIN submission_current s ON s.id = e.project_id "
    "JOIN organisations o ON o.id = s.org_id LEFT JOIN users r ON r.id = e.rated_by"
)


def my_history(db: Database, user_id: str) -> dict:
    """The volunteer's own engagements, newest first, with every rating about them, and which
    of their organisations may see a summary of what they did elsewhere."""
    with db.read() as conn:
        rows = conn.execute(
            f"SELECT {_COLUMNS} {_JOINS} WHERE e.volunteer_id = ? "  # noqa: S608
            "ORDER BY e.ended_at IS NOT NULL, e.started_at DESC, e.id",
            (user_id,),
        ).fetchall()
        skills = _skills(conn, "e.volunteer_id = ?", (user_id,))
        sharing = conn.execute(
            "SELECT m.org_id, o.name AS org_name, m.share_history FROM memberships m "
            "JOIN organisations o ON o.id = m.org_id WHERE m.user_id = ? AND m.opted_in = 1 "
            "ORDER BY o.name, o.id",
            (user_id,),
        ).fetchall()
    return {
        "engagements": [_shape(r, skills) for r in rows],
        "sharing": [dict(r) | {"share_history": bool(r["share_history"])} for r in sharing],
        "levels": LEVELS,
    }


def project_history(db: Database, actor_id: str, org_id: str) -> list[dict]:
    """For the lead side: the organisation's completed projects that the actor acts for (all of
    them for a team coordinator or org admin, their own for a lead), each with its engagements
    and whether the actor may rate them. Carries the lead's private note."""
    access = require_access(db, actor_id, org_id)
    with db.read() as conn:
        projects = [
            dict(r)
            for r in conn.execute(
                "SELECT s.id, s.title, s.lead_id, sub.completed_at, sub.approved_revision "
                "FROM submission_current s JOIN submissions sub ON sub.id = s.id "
                "WHERE s.org_id = ? AND s.state = 'completed' ORDER BY sub.completed_at DESC, s.id",
                (org_id,),
            )
            if access.team_coordinator
            or (r["lead_id"] == actor_id and teams.in_standing(conn, actor_id, org_id))
        ]
        if not projects:
            return []
        ids = [p["id"] for p in projects]
        marks = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT {_COLUMNS}, e.private_note, u.display_name {_JOINS} "  # noqa: S608
            f"JOIN users u ON u.id = e.volunteer_id WHERE e.project_id IN ({marks}) "
            "ORDER BY u.display_name, e.started_at, e.id",
            ids,
        ).fetchall()
        skills = _skills(conn, f"e.project_id IN ({marks})", tuple(ids))
        project_skills: dict[str, list[dict]] = {}
        cat = get_catalogue()
        for r in conn.execute(
            "SELECT k.submission_id, k.skill_id FROM revision_skills k JOIN submissions s "
            "ON s.id = k.submission_id AND s.approved_revision = k.revision "
            f"WHERE s.id IN ({marks}) ORDER BY k.skill_id",  # noqa: S608
            ids,
        ):
            project_skills.setdefault(r["submission_id"], []).append(
                {"id": r["skill_id"], "name": cat.skill_name(r["skill_id"])}
            )
    for project in projects:
        project["skills"] = project_skills.get(project["id"], [])
        project["engagements"] = [
            _shape(r, skills)
            | {"display_name": r["display_name"], "private_note": r["private_note"],
               "can_rate": r["volunteer_id"] != actor_id and r["ended_at"] is not None}
            for r in rows
            if r["project_id"] == project["id"]
        ]  # fmt: skip
    return projects


def set_share_history(db: Database, user_id: str, org_id: str, *, share: bool) -> None:
    """The volunteer decides whether one of their organisations may see a summary of what they
    did with others. Only they can change it."""
    with db.tx() as conn:
        if not org_access(conn, user_id, org_id).volunteer:
            raise Forbidden("volunteer")
        conn.execute(
            "UPDATE memberships SET share_history = ? WHERE user_id = ? AND org_id = ?",
            (int(share), user_id, org_id),
        )
        enqueue(conn, "volunteer", user_id)
