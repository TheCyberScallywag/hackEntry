"""Volunteer onboarding, step 1: screening by a moderator or system admin."""

from app.notify import notify, notify_vetters
from app.services.access import require_vetter
from app.services.errors import Conflict, Forbidden, Invalid, NotFound, require_reason
from app.services.profile import get_profile
from app.sqlstore import Database, enqueue, now

VOLUNTEER_STATUS_LABELS = {
    "draft": "Not yet submitted for vetting",
    "screening": "In screening",
    "info_requested": "More information requested",
    "active": "Active",
    "declined": "Not approved",
    "suspended": "Suspended",
}
# From these statuses the volunteer may (re)submit their profile for screening.
SUBMITTABLE = ("draft", "info_requested", "declined")
VETTING_ACTIONS = {  # action -> (allowed from, to)
    "approve": (("screening", "info_requested"), "active"),
    "request_info": (("screening",), "info_requested"),
    "decline": (("screening", "info_requested"), "declined"),
    "suspend": (("active",), "suspended"),
    "reinstate": (("suspended",), "active"),
}
VETTING_MESSAGES = {
    "screening": "Your volunteer profile is in screening. A moderator will review it soon.",
    "info_requested": "A moderator asked for more information before approving you",
    "active": "You are approved and active. You can now be matched to projects",
    "declined": "Your volunteer application was not approved",
    "suspended": "Your account has been suspended",
}


def submit_for_vetting(db: Database, user_id: str) -> None:
    """Step 1 of onboarding: the volunteer submits their completed profile for screening."""
    with db.tx() as conn:
        user = conn.execute("SELECT status FROM users WHERE id = ?", (user_id,)).fetchone()
        if user is None:
            raise NotFound("user")
        if user["status"] not in SUBMITTABLE:
            raise Conflict("Your profile is already in screening or active.")
        offered = conn.execute(
            "SELECT count(*) FROM profile_skills WHERE user_id = ? AND offers = 1", (user_id,)
        ).fetchone()[0]
        if not offered:
            raise Invalid("Offer at least one skill on your profile before submitting.")
        _set_status(conn, user_id, user_id, user["status"], "screening", "", "")
        notify_vetters(conn, "vetting", "A volunteer profile is waiting for screening.",
                       "/admin")  # fmt: skip


def _set_status(conn, user_id, actor_id, old, new, note, private_note) -> None:
    stamp = now()
    conn.execute(
        "UPDATE users SET status = ?, status_version = status_version + 1, "
        "vetted_by = CASE WHEN ? = 'active' THEN ? ELSE vetted_by END, "
        "vetted_at = CASE WHEN ? = 'active' THEN ? ELSE vetted_at END WHERE id = ?",
        (new, new, actor_id, new, stamp, user_id),
    )
    conn.execute(
        "INSERT INTO vetting_events (user_id, actor_id, from_status, to_status, "
        "note_for_volunteer, private_note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, actor_id, old, new, note, private_note, stamp),
    )
    message = VETTING_MESSAGES[new] + (f": {note}" if note else ".")
    notify(conn, user_id, "vetting", message.replace("..", "."), "/profile")
    enqueue(conn, "volunteer", user_id)  # only active volunteers get membership edges


def vetting_queue(db: Database, actor_id: str, status: str = "screening") -> list[dict]:
    require_vetter(db, actor_id)
    if status not in VOLUNTEER_STATUS_LABELS:
        raise Invalid("Unknown status.")
    with db.read() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT u.id, u.display_name, u.status, u.status_version, u.created_at, "
                "(SELECT count(*) FROM profile_skills ps WHERE ps.user_id = u.id "
                " AND ps.offers = 1) AS skills_offered "
                "FROM users u WHERE u.status = ? ORDER BY u.created_at, u.id",
                (status,),
            )
        ]


def vetting_record(db: Database, actor_id: str, user_id: str) -> dict:
    """What a moderator sees: profile, memberships and history. Emails only for admins."""
    actor = require_vetter(db, actor_id)
    profile = get_profile(db, user_id)
    with db.read() as conn:
        user = dict(
            conn.execute(
                "SELECT id, display_name, email, status, status_version, created_at, vetted_at "
                "FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        )
        if not actor.admin:
            user.pop("email")
        orgs = [
            r[0]
            for r in conn.execute(
                "SELECT o.name FROM memberships m JOIN organisations o ON o.id = m.org_id "
                "WHERE m.user_id = ? AND m.opted_in = 1 ORDER BY o.name",
                (user_id,),
            )
        ]
        history = [
            dict(r)
            for r in conn.execute(
                "SELECT e.from_status, e.to_status, e.note_for_volunteer, e.private_note, "
                "e.created_at, a.display_name AS actor FROM vetting_events e "
                "JOIN users a ON a.id = e.actor_id WHERE e.user_id = ? ORDER BY e.id",
                (user_id,),
            )
        ]
    return {**user, "skills": profile.skills, "causes": profile.causes, "organisations": orgs,
            "history": history}  # fmt: skip


def vet_volunteer(
    db: Database, actor_id: str, user_id: str, *, action: str, note: str = "",
    private_note: str = "", expected_version: int,
) -> str:  # fmt: skip
    """A moderator or admin moves a volunteer through screening. Returns the new status."""
    if action not in VETTING_ACTIONS:
        raise Invalid("Unknown vetting action.")
    note, private_note = note.strip(), private_note.strip()
    if action != "approve":
        note = require_reason(note)
    require_vetter(db, actor_id)
    if actor_id == user_id:
        raise Forbidden("Moderators cannot vet themselves.")
    allowed_from, new = VETTING_ACTIONS[action]
    with db.tx() as conn:
        user = conn.execute(
            "SELECT status, status_version FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if user is None:
            raise NotFound("user")
        if user["status"] not in allowed_from or user["status_version"] != expected_version:
            raise Conflict("This volunteer's status changed since you opened it.")
        _set_status(conn, user_id, actor_id, user["status"], new, note, private_note)
    return new
