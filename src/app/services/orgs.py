"""Organisations: joining and leaving, an organisation's volunteers, and creating one."""

import re
import sqlite3

from app.notify import notify
from app.services import onboarding, teams
from app.services.access import (
    get_org,
    load_actor,
    load_directory,
    require_access,
    require_admin,
)
from app.services.errors import Conflict, Invalid, NotFound, require_reason
from app.sqlstore import Database, enqueue, new_id, now


def set_membership(db: Database, user_id: str, org_id: str, *, opted_in: bool) -> None:
    """Join (opt in) or leave (opt out). Distinct from switching the active organisation.

    Leaving also gives up the volunteer's team places and open offers in that organisation."""
    with db.tx() as conn:
        org = get_org(conn, org_id)
        was_in = conn.execute(
            "SELECT 1 FROM memberships WHERE user_id = ? AND org_id = ? AND opted_in = 1",
            (user_id, org_id),
        ).fetchone()
        conn.execute(
            "INSERT INTO memberships (user_id, org_id, opted_in, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (user_id, org_id) DO UPDATE SET opted_in = excluded.opted_in, "
            "updated_at = excluded.updated_at",
            (user_id, org_id, int(opted_in), now()),
        )
        if opted_in and load_actor(conn, user_id).status != "active":
            notify(
                conn, user_id, "membership",
                f"You joined {org['name']}. You will be matched to its projects once your "
                "account has been vetted.", "/profile",
            )  # fmt: skip
        if opted_in and not was_in:
            onboarding.start(conn, user_id, org_id)  # the organisation's own workflow, if on
        if not opted_in:
            onboarding.cancel(conn, user_id, org_id)
            teams.end_for_volunteer(
                conn, user_id, org_id=org_id, team_state="left", event="left_organisation",
                actor_id=user_id,
            )  # fmt: skip
            teams.flag_leaderless(conn, user_id, org_id)
        enqueue(conn, "volunteer", user_id)


def org_volunteers(db: Database, actor_id: str, org_id: str) -> list[dict]:
    """An organisation's opted-in volunteers, for its org admins (no emails)."""
    require_access(db, actor_id, org_id, role="org_admin")
    with db.read() as conn:
        rows = conn.execute(
            "SELECT u.id, u.display_name, u.status, m.updated_at AS joined_at, "
            "(SELECT group_concat(ps.skill_id) FROM profile_skills ps WHERE ps.user_id = u.id "
            " AND ps.offers = 1) AS offers, "
            "(SELECT count(*) FROM placements p JOIN submissions s ON s.id = p.project_id "
            " WHERE p.volunteer_id = u.id AND p.state = 'team' AND s.org_id = ?) AS teams "
            "FROM memberships m JOIN users u ON u.id = m.user_id "
            "WHERE m.org_id = ? AND m.opted_in = 1 ORDER BY u.display_name, u.id",
            (org_id, org_id),
        ).fetchall()
    return [{**dict(r), "offers": sorted(r["offers"].split(",")) if r["offers"] else []}
            for r in rows]  # fmt: skip


def remove_volunteer(
    db: Database, actor_id: str, org_id: str, user_id: str, *, reason: str
) -> None:
    """An org admin removes a volunteer from their organisation (and its project teams)."""
    reason = require_reason(reason)
    require_access(db, actor_id, org_id, role="org_admin")
    with db.tx() as conn:
        org = get_org(conn, org_id)
        stamp = now()
        updated = conn.execute(
            "UPDATE memberships SET opted_in = 0, updated_at = ? "
            "WHERE user_id = ? AND org_id = ? AND opted_in = 1",
            (stamp, user_id, org_id),
        ).rowcount
        if not updated:
            raise NotFound("volunteer")
        teams.end_for_volunteer(
            conn, user_id, org_id=org_id, team_state="removed", event="removed_from_organisation",
            actor_id=actor_id, note=reason,
        )  # fmt: skip
        teams.flag_leaderless(conn, user_id, org_id)
        onboarding.cancel(conn, user_id, org_id)
        notify(conn, user_id, "membership",
               f"{org['name']} removed you as a volunteer: {reason}", "/orgs")  # fmt: skip
        enqueue(conn, "volunteer", user_id)


def create_organisation(
    db: Database, actor_id: str, *, key: str, name: str, description: str
) -> str:
    require_admin(db, actor_id)
    key, name = key.strip().lower(), name.strip()
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,40}", key) or not 2 <= len(name) <= 80:
        raise Invalid("Key must be lower-case letters, digits and hyphens; name 2–80 characters.")
    org_id = new_id("org")
    try:
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO organisations (id, key, name, description) VALUES (?, ?, ?, ?)",
                (org_id, key, name, description.strip()[:500]),
            )
            onboarding.install_starter(conn, org_id)
            enqueue(conn, "organisation", org_id)
    except sqlite3.IntegrityError as error:
        raise Conflict("An organisation with that key already exists.") from error
    return org_id


def _ids(joined: str | None) -> list[str]:
    return sorted(joined.split(",")) if joined else []


def showcase(db: Database, user_id: str) -> list[dict]:
    """Every organisation with its published projects, to promote them to prospective volunteers.

    Any signed-in user may read this, member or not, so each project carries only what a
    recruitment notice would: title, summary, cause and skills. Leads, teams, candidates and
    matches stay behind `require_access`. `opted_in` and `reviewer` say whether this user may
    open the project pages.
    """
    with db.read() as conn:
        orgs = load_directory(conn, user_id, admin=load_actor(conn, user_id).admin)
        projects = conn.execute(
            "SELECT s.id, s.org_id, v.title, v.summary, v.cause_id, "
            "(SELECT group_concat(k.skill_id) FROM revision_skills k WHERE k.submission_id = s.id "
            " AND k.revision = v.revision AND k.required = 1) AS required, "
            "(SELECT group_concat(k.skill_id) FROM revision_skills k WHERE k.submission_id = s.id "
            " AND k.revision = v.revision AND k.required = 0) AS optional "
            "FROM submissions s JOIN submission_revisions v "
            "  ON v.submission_id = s.id AND v.revision = s.approved_revision "
            "WHERE s.state = 'published' ORDER BY v.title, s.id"
        ).fetchall()
    by_org: dict[str, list[dict]] = {o["id"]: [] for o in orgs}
    for row in projects:
        by_org[row["org_id"]].append(
            {**dict(row), "required": _ids(row["required"]), "optional": _ids(row["optional"])}
        )
    return [{**o, "projects": by_org[o["id"]]} for o in orgs]
