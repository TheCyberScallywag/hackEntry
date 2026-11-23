"""Team places and standing: the rules shared by placements, vetting, organisations and projects.

A volunteer is *in standing* with an organisation while their account is active and they are
opted in to it. Only then can they hold an open offer or a place on a team there. A project lead
keeps the lead's powers only while in standing (or while they are an org admin).

Everything here runs inside the caller's transaction and writes a `placement_events` row for
each change, so a placement's history says who did what and why.
"""

import sqlite3

from app.notify import notify
from app.services.access import org_access
from app.services.errors import Forbidden, NotFound
from app.sqlstore import enqueue, now

# Placement states in which a volunteer holds a place on a team or is on the way to one:
# going through the project's onboarding, on the team, or suspended from it.
HOLDING = ("onboarding", "team", "suspended")


def record(
    conn: sqlite3.Connection,
    project_id: str,
    volunteer_id: str,
    event: str,
    *,
    actor_id: str | None = None,
    note: str = "",
) -> None:
    """One line of a placement's history. `actor_id` None means the system did it."""
    conn.execute(
        "INSERT INTO placement_events (project_id, volunteer_id, actor_id, event, note, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (project_id, volunteer_id, actor_id, event, note, now()),
    )


def in_standing(conn: sqlite3.Connection, user_id: str, org_id: str) -> bool:
    """Active account and opted in to the organisation."""
    return (
        conn.execute(
            "SELECT 1 FROM users u JOIN memberships m ON m.user_id = u.id "
            "WHERE u.id = ? AND u.status = 'active' AND m.org_id = ? AND m.opted_in = 1",
            (user_id, org_id),
        ).fetchone()
        is not None
    )


def lead_in_standing(conn: sqlite3.Connection, lead_id: str | None, org_id: str) -> bool:
    """Whether the project's lead may still act for it: in standing, or an org admin."""
    if not lead_id:
        return False
    return org_access(conn, lead_id, org_id).org_admin or in_standing(conn, lead_id, org_id)


def project_info(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT s.id, s.org_id, s.lead_id, s.state, s.title, o.name AS org_name "
        "FROM submission_current s JOIN organisations o ON o.id = s.org_id WHERE s.id = ?",
        (project_id,),
    ).fetchone()


def check_lead_side(
    conn: sqlite3.Connection, actor_id: str, project_id: str, volunteer_id: str = ""
) -> sqlite3.Row:
    """The actor may act for the project's lead side: an org admin (or system admin), or the
    project's lead while they are still in standing with the organisation. Org admins assign
    the lead (app.services.submissions.set_project_lead), so a lead's powers come from, and
    stay inside, the organisation.

    With a `volunteer_id`, the actor must also not be deciding on their own place; only the
    project's lead may, since the lead is on their own team anyway."""
    info = project_info(conn, project_id)
    if info is None:
        raise NotFound("project")
    access = org_access(conn, actor_id, info["org_id"])
    is_lead = info["lead_id"] == actor_id and in_standing(conn, actor_id, info["org_id"])
    if not (is_lead or access.org_admin):
        if access.any:
            raise Forbidden("Only the project lead or an org admin can do that.")
        raise NotFound("project")
    if volunteer_id and volunteer_id == actor_id and info["lead_id"] != actor_id:
        raise Forbidden("Someone else must decide on your own place on a team.")
    return info


def end_project_run(conn: sqlite3.Connection, volunteer_id: str, project_id: str) -> None:
    """The volunteer's place on the project has ended, so their run through the project's own
    onboarding ends with it. Coming back starts a new one. The caller queues the projection."""
    conn.execute(
        "UPDATE onboarding_runs SET state = 'cancelled' "
        "WHERE user_id = ? AND project_id = ? AND state != 'cancelled'",
        (volunteer_id, project_id),
    )


def notify_deciders(
    conn: sqlite3.Connection,
    org_id: str,
    lead_id: str | None,
    message: str,
    link: str,
    *,
    exclude: tuple[str | None, ...] = (),
    also: tuple[str | None, ...] = (),
) -> None:
    """Tell whoever decides for a project: its lead while they are in standing, otherwise the
    organisation's admins, so a project with no active lead does not stall unnoticed. `also`
    adds people (say, the org admin who sent an invitation)."""
    if lead_in_standing(conn, lead_id, org_id):
        targets = {lead_id}
    else:
        targets = {
            r[0]
            for r in conn.execute(
                "SELECT user_id FROM org_roles WHERE org_id = ? AND role = 'org_admin'", (org_id,)
            )
        }
    for user_id in sorted((targets | set(also)) - set(exclude) - {None}):
        notify(conn, user_id, "placement", message, link)


def project_link(org_id: str, project_id: str) -> str:
    return f"/o/{org_id}/projects/{project_id}"


def ensure_lead_on_team(conn: sqlite3.Connection, project_id: str) -> bool:
    """The lead of a live project is on its team automatically, with no second person needed.

    Applies while the lead is in standing as a volunteer and has no placement yet, or only an
    open or lapsed one. A lead who left the team, or was removed from it, is not put back.
    """
    sub = conn.execute(
        "SELECT id, org_id, lead_id, state, title FROM submission_current WHERE id = ?",
        (project_id,),
    ).fetchone()
    if sub is None or sub["state"] not in ("approved", "published") or not sub["lead_id"]:
        return False
    lead = sub["lead_id"]
    if not in_standing(conn, lead, sub["org_id"]):
        return False
    row = conn.execute(
        "SELECT state FROM placements WHERE project_id = ? AND volunteer_id = ?",
        (project_id, lead),
    ).fetchone()
    if row and row["state"] not in ("eligible", "lapsed"):
        return False
    stamp = now()
    conn.execute(
        "INSERT INTO placements (project_id, volunteer_id, state, volunteer_said, "
        "volunteer_said_at, lead_said, lead_said_at, lead_actor_id, invited_at, created_at, "
        "updated_at) VALUES (?, ?, 'team', 'yes', ?, 'yes', ?, ?, ?, ?, ?) "
        "ON CONFLICT (project_id, volunteer_id) DO UPDATE SET state = 'team', "
        "volunteer_said = 'yes', volunteer_said_at = excluded.volunteer_said_at, "
        "lead_said = 'yes', lead_said_at = excluded.lead_said_at, "
        "lead_actor_id = excluded.lead_actor_id, lead_note = '', "
        "invited_at = COALESCE(invited_at, excluded.invited_at), "
        "version = version + 1, updated_at = excluded.updated_at",
        (project_id, lead, stamp, stamp, lead, stamp, stamp, stamp),
    )
    record(conn, project_id, lead, "lead_joined", actor_id=lead)
    notify(conn, lead, "placement", f"You lead “{sub['title']}”, so you are on its team.",
           project_link(sub["org_id"], project_id))  # fmt: skip
    return True


def end_for_volunteer(
    conn: sqlite3.Connection,
    volunteer_id: str,
    *,
    org_id: str | None = None,
    team_state: str,
    event: str,
    actor_id: str | None,
    note: str = "",
) -> None:
    """A volunteer lost their standing (suspended, left or removed from an organisation): close
    their open offers and take them off their teams, in one organisation or in all of them.
    A place they were still onboarding for, or were suspended from, ends the same way.

    Team places end in `team_state` ('left' or 'removed'); a lead or org admin can reopen them
    later. Open offers become 'lapsed', so they come back by themselves if the volunteer
    returns and still matches. The caller tells the volunteer; this tells each project's lead.
    """
    rows = conn.execute(
        "SELECT p.project_id, p.state, s.title, s.org_id, s.lead_id FROM placements p "
        "JOIN submission_current s ON s.id = p.project_id "
        "WHERE p.volunteer_id = ? AND p.state IN ('eligible', 'onboarding', 'team', 'suspended') "
        "AND (? IS NULL OR s.org_id = ?) ORDER BY p.project_id",
        (volunteer_id, org_id, org_id),
    ).fetchall()
    if not rows:
        return
    name = conn.execute("SELECT display_name FROM users WHERE id = ?", (volunteer_id,)).fetchone()[
        0
    ]
    stamp = now()
    for row in rows:
        on_team = row["state"] in HOLDING
        conn.execute(
            "UPDATE placements SET state = ?, version = version + 1, updated_at = ? "
            "WHERE project_id = ? AND volunteer_id = ?",
            (team_state if on_team else "lapsed", stamp, row["project_id"], volunteer_id),
        )
        record(conn, row["project_id"], volunteer_id, event, actor_id=actor_id, note=note)
        if on_team:
            end_project_run(conn, volunteer_id, row["project_id"])
            notify_deciders(
                conn, row["org_id"], row["lead_id"],
                f"{name} is no longer on the team for “{row['title']}”.",
                project_link(row["org_id"], row["project_id"]),
                exclude=(volunteer_id, actor_id),
            )  # fmt: skip
            enqueue(conn, "project", row["project_id"])  # removes the ON_TEAM edge


def flag_leaderless(conn: sqlite3.Connection, user_id: str, org_id: str | None = None) -> None:
    """Tell the org admins of each live project this user leads, and can no longer act for,
    that it needs a new lead."""
    for row in conn.execute(
        "SELECT id, org_id, title FROM submission_current WHERE lead_id = ? "
        "AND state IN ('approved', 'published') AND (? IS NULL OR org_id = ?) ORDER BY id",
        (user_id, org_id, org_id),
    ).fetchall():
        if lead_in_standing(conn, user_id, row["org_id"]):
            continue
        for (admin,) in conn.execute(
            "SELECT user_id FROM org_roles WHERE org_id = ? AND role = 'org_admin' "
            "AND user_id != ? ORDER BY user_id",
            (row["org_id"], user_id),
        ).fetchall():
            notify(conn, admin, "placement",
                   f"“{row['title']}” has no active lead any more. Assign a new one; until "
                   "then only org admins can invite its candidates.",
                   project_link(row["org_id"], row["id"]))  # fmt: skip
