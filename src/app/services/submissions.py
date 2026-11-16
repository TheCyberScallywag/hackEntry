"""Project onboarding: a suggestion, its revisions, review, publication and withdrawal."""

import sqlite3
from dataclasses import dataclass

from app.catalogue import get_catalogue
from app.notify import notify, notify_org_admins
from app.services.access import load_actor, org_access, require_access, require_active
from app.services.errors import (
    NOTE_MAX,
    SUMMARY_MAX,
    TITLE_MAX,
    Conflict,
    Forbidden,
    Invalid,
    NotFound,
    require_reason,
)
from app.sqlstore import Database, enqueue, new_id, now

# State -> label, for the web templates, the API and the mobile app alike.
PROJECT_STATES = {
    "submitted": "In review",
    "changes_requested": "Scoping: more detail requested",
    "rejected": "Rejected",
    "approved": "Approved (activating…)",
    "published": "Active",
    "withdrawn": "Withdrawn",
}
REVIEW_ACTIONS = ("changes_requested", "rejected", "approved")


@dataclass
class Draft:
    title: str
    summary: str
    cause_id: str
    required: list[str]
    optional: list[str]

    def validate(self) -> Draft:
        catalogue = get_catalogue()
        self.title, self.summary = self.title.strip(), self.summary.strip()
        self.required = sorted(set(self.required))
        self.optional = sorted(set(self.optional) - set(self.required))
        if not 3 <= len(self.title) <= TITLE_MAX:
            raise Invalid(f"Title must be 3–{TITLE_MAX} characters.")
        if not 10 <= len(self.summary) <= SUMMARY_MAX:
            raise Invalid(f"Summary must be 10–{SUMMARY_MAX} characters.")
        if self.cause_id not in catalogue.cause_ids:
            raise Invalid("Choose a cause from the catalogue.")
        if not self.required:
            raise Invalid("Choose at least one required skill.")
        if set(self.required + self.optional) - catalogue.skill_ids:
            raise Invalid("Choose skills from the shared catalogue.")
        return self


def _insert_revision(conn: sqlite3.Connection, sid: str, revision: int, draft: Draft) -> None:
    conn.execute(
        "INSERT INTO submission_revisions (submission_id, revision, title, summary, cause_id, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (sid, revision, draft.title, draft.summary, draft.cause_id, now()),
    )
    conn.executemany(
        "INSERT INTO revision_skills (submission_id, revision, skill_id, required) "
        "VALUES (?, ?, ?, ?)",
        [(sid, revision, s, 1) for s in draft.required]
        + [(sid, revision, s, 0) for s in draft.optional],
    )


def create_submission(db: Database, user_id: str, org_id: str, draft: Draft) -> str:
    """Project onboarding, step 1: an active user suggests a project to one organisation they
    belong to. It goes to that organisation's admin team for review."""
    draft.validate()
    require_active(db, user_id)
    require_access(db, user_id, org_id)
    sid = new_id("p")
    stamp = now()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO submissions (id, org_id, author_id, lead_id, state, current_revision, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, 'submitted', 1, ?, ?)",
            (sid, org_id, user_id, user_id, stamp, stamp),
        )
        _insert_revision(conn, sid, 1, draft)
        link = f"/submissions/{sid}"
        notify(conn, user_id, "project", f"“{draft.title}” was submitted and is in review.", link)
        notify_org_admins(conn, org_id, "review", f"New project suggestion: “{draft.title}”.", link)
    return sid


def revise_submission(
    db: Database, user_id: str, sid: str, draft: Draft, *, expected_version: int
) -> None:
    """The author resubmits after changes were requested; starts a new revision and tells the
    organisation's reviewers it is back in their queue."""
    draft.validate()
    with db.tx() as conn:
        sub = conn.execute(
            "SELECT org_id, author_id, state, current_revision, version FROM submissions "
            "WHERE id = ?",
            (sid,),
        ).fetchone()
        if sub is None or sub["author_id"] != user_id:
            raise NotFound("submission")
        if sub["state"] != "changes_requested" or sub["version"] != expected_version:
            raise Conflict("This suggestion is not awaiting your changes any more.")
        revision = sub["current_revision"] + 1
        _insert_revision(conn, sid, revision, draft)
        conn.execute(
            "UPDATE submissions SET state = 'submitted', current_revision = ?, "
            "version = version + 1, updated_at = ? WHERE id = ? AND version = ?",
            (revision, now(), sid, expected_version),
        )
        notify_org_admins(
            conn, sub["org_id"], "review",
            f"“{draft.title}” was revised and is back in review.", f"/submissions/{sid}",
        )  # fmt: skip


def _revision(conn: sqlite3.Connection, sid: str, revision: int) -> dict:
    rev = conn.execute(
        "SELECT revision, title, summary, cause_id, created_at FROM submission_revisions "
        "WHERE submission_id = ? AND revision = ?",
        (sid, revision),
    ).fetchone()
    skills = conn.execute(
        "SELECT skill_id, required FROM revision_skills WHERE submission_id = ? AND revision = ? "
        "ORDER BY skill_id",
        (sid, revision),
    ).fetchall()
    return {
        **dict(rev),
        "required": [s["skill_id"] for s in skills if s["required"]],
        "optional": [s["skill_id"] for s in skills if not s["required"]],
    }


def get_submission(db: Database, user_id: str, sid: str) -> dict:
    """Visible to its author and to reviewers of its organisation; anyone else gets NotFound.
    Private review notes are included only for reviewers."""
    with db.read() as conn:
        # Columns are named, so a column added to `submissions` is not published by accident.
        sub = conn.execute(
            "SELECT s.id, s.org_id, s.author_id, s.lead_id, s.state, s.current_revision, "
            "s.version, s.approved_revision, s.approved_by, s.approved_at, s.published_at, "
            "s.withdrawn_at, s.created_at, s.updated_at, o.name AS org_name, "
            "u.display_name AS author_name, a.display_name AS approved_by_name "
            "FROM submissions s JOIN organisations o ON o.id = s.org_id "
            "JOIN users u ON u.id = s.author_id LEFT JOIN users a ON a.id = s.approved_by "
            "WHERE s.id = ?",
            (sid,),
        ).fetchone()
        if sub is None:
            raise NotFound("submission")
        is_author = sub["author_id"] == user_id
        is_reviewer = org_access(conn, user_id, sub["org_id"]).reviewer
        if not (is_author or is_reviewer):
            raise NotFound("submission")
        reviews = [
            {
                **dict(r),
                "private_note": r["private_note"] if is_reviewer else "",
            }
            for r in conn.execute(
                "SELECT r.revision, r.action, r.reason_for_author, r.private_note, r.created_at, "
                "u.display_name AS reviewer_name FROM reviews r JOIN users u "
                "ON u.id = r.reviewer_id WHERE r.submission_id = ? ORDER BY r.id",
                (sid,),
            )
        ]
        current = _revision(conn, sid, sub["current_revision"])
    return {
        **dict(sub),
        "current": current,
        "reviews": reviews,
        "is_author": is_author,
        "is_reviewer": is_reviewer,
        "can_review": is_reviewer and not is_author and sub["state"] == "submitted",
        "can_revise": is_author and sub["state"] == "changes_requested",
        "can_withdraw": is_reviewer and sub["state"] in ("approved", "published"),
    }


def my_submissions(db: Database, user_id: str) -> list[dict]:
    with db.read() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT s.id, s.org_id, s.state, s.current_revision, s.updated_at, "
                "o.name AS org_name, s.title FROM submission_current s "
                "JOIN organisations o ON o.id = s.org_id WHERE s.author_id = ? "
                "ORDER BY s.updated_at DESC, s.id",
                (user_id,),
            )
        ]


def review_queue(db: Database, user_id: str, org_id: str) -> list[dict]:
    require_access(db, user_id, org_id, role="reviewer")
    with db.read() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT s.id, s.state, s.current_revision, s.updated_at, s.author_id, "
                "u.display_name AS author_name, s.title FROM submission_current s "
                "JOIN users u ON u.id = s.author_id WHERE s.org_id = ? "
                "ORDER BY CASE s.state WHEN 'submitted' THEN 0 ELSE 1 END, s.updated_at DESC, s.id",
                (org_id,),
            )
        ]


def review_counts(db: Database, user_id: str) -> dict[str, int]:
    """Suggestions awaiting review, per organisation this user reviews for (zero included)."""
    with db.read() as conn:
        admin = load_actor(conn, user_id).admin
        return dict(
            conn.execute(
                "SELECT o.id, (SELECT count(*) FROM submissions s WHERE s.org_id = o.id "
                "AND s.state = 'submitted') FROM organisations o WHERE ? OR EXISTS "
                "(SELECT 1 FROM org_roles r WHERE r.user_id = ? AND r.org_id = o.id)",
                (int(admin), user_id),
            ).fetchall()
        )


def review_submission(
    db: Database,
    reviewer_id: str,
    org_id: str,
    sid: str,
    *,
    action: str,
    reason: str,
    private_note: str = "",
    revision: int,
    expected_version: int,
) -> None:
    """Decide on the current revision. The WHERE clause re-checks state, revision and version,
    so a stale or replayed form cannot publish twice or approve an outdated revision."""
    if action not in REVIEW_ACTIONS:
        raise Invalid("Unknown review action.")
    reason = require_reason(reason, "Give the author a reason")
    private_note = private_note.strip()
    if len(private_note) > NOTE_MAX:
        raise Invalid("Private note is too long.")
    require_access(db, reviewer_id, org_id, role="reviewer")
    stamp = now()
    with db.tx() as conn:
        sub = conn.execute(
            "SELECT author_id, state, current_revision, version FROM submissions "
            "WHERE id = ? AND org_id = ?",
            (sid, org_id),
        ).fetchone()
        if sub is None:
            raise NotFound("submission")
        if sub["author_id"] == reviewer_id:
            raise Forbidden("Reviewers cannot review their own suggestions.")
        updated = conn.execute(
            "UPDATE submissions SET state = ?, version = version + 1, updated_at = ?, "
            "approved_revision = CASE WHEN ? = 'approved' THEN current_revision "
            "                    ELSE approved_revision END, "
            "approved_by = CASE WHEN ? = 'approved' THEN ? ELSE approved_by END, "
            "approved_at = CASE WHEN ? = 'approved' THEN ? ELSE approved_at END "
            "WHERE id = ? AND org_id = ? AND state = 'submitted' AND current_revision = ? "
            "AND version = ?",
            (action, stamp, action, action, reviewer_id, action, stamp, sid, org_id,
             revision, expected_version),
        ).rowcount  # fmt: skip
        if not updated:
            raise Conflict("This suggestion changed since you opened it. Reload to see why.")
        conn.execute(
            "INSERT INTO reviews (submission_id, revision, reviewer_id, action, "
            "reason_for_author, private_note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sid, revision, reviewer_id, action, reason, private_note, stamp),
        )
        if action == "approved":
            enqueue(conn, "project", sid)
        title = conn.execute(
            "SELECT title FROM submission_revisions WHERE submission_id = ? AND revision = ?",
            (sid, revision),
        ).fetchone()[0]
        message = {
            "changes_requested": f"“{title}” is in scoping: more detail requested. {reason}",
            "rejected": f"“{title}” was not approved: {reason}",
            "approved": f"“{title}” was approved and is being activated.",
        }[action]
        notify(conn, sub["author_id"], "project", message, f"/submissions/{sid}")


def withdraw_project(
    db: Database, reviewer_id: str, org_id: str, sid: str, *, reason: str, expected_version: int
) -> None:
    reason = require_reason(reason, "Give a reason for withdrawing")
    require_access(db, reviewer_id, org_id, role="reviewer")
    stamp = now()
    with db.tx() as conn:
        sub = conn.execute(
            "SELECT author_id, current_revision, title FROM submission_current "
            "WHERE id = ? AND org_id = ?",
            (sid, org_id),
        ).fetchone()
        if sub is None:
            raise NotFound("submission")
        updated = conn.execute(
            "UPDATE submissions SET state = 'withdrawn', version = version + 1, "
            "withdrawn_by = ?, withdrawn_at = ?, updated_at = ? WHERE id = ? AND org_id = ? "
            "AND state IN ('approved', 'published') AND version = ?",
            (reviewer_id, stamp, stamp, sid, org_id, expected_version),
        ).rowcount
        if not updated:
            raise Conflict("This project is not published any more, or it changed.")
        conn.execute(
            "INSERT INTO reviews (submission_id, revision, reviewer_id, action, "
            "reason_for_author, created_at) VALUES (?, ?, ?, 'withdrawn', ?, ?)",
            (sid, sub["current_revision"], reviewer_id, reason, stamp),
        )
        notify(conn, sub["author_id"], "project", f"“{sub['title']}” was withdrawn: {reason}",
               f"/submissions/{sid}")  # fmt: skip
        affected = conn.execute(
            "SELECT volunteer_id FROM placements WHERE project_id = ? "
            "AND state IN ('eligible', 'team')",
            (sid,),
        ).fetchall()
        conn.execute(
            "UPDATE placements SET state = 'removed', version = version + 1, updated_at = ? "
            "WHERE project_id = ? AND state IN ('eligible', 'team')",
            (stamp, sid),
        )
        for (vid,) in affected:
            notify(conn, vid, "placement",
                   f"“{sub['title']}” was withdrawn, so your place on it has ended.",
                   "/opportunities")  # fmt: skip
        enqueue(conn, "project", sid)


def set_project_lead(db: Database, actor_id: str, org_id: str, sid: str, lead_id: str) -> None:
    """An org admin hands a project to a new lead: an active volunteer or admin of the org."""
    require_access(db, actor_id, org_id, role="org_admin")
    with db.tx() as conn:
        sub = conn.execute(
            "SELECT 1 FROM submissions WHERE id = ? AND org_id = ? "
            "AND state IN ('approved', 'published')",
            (sid, org_id),
        ).fetchone()
        if sub is None:
            raise NotFound("project")
        access = org_access(conn, lead_id, org_id)
        if not (access.org_admin or (access.volunteer and load_actor(conn, lead_id).active)):
            raise Invalid("The lead must be an active volunteer or an admin of this organisation.")
        conn.execute(
            "UPDATE submissions SET lead_id = ?, version = version + 1, updated_at = ? "
            "WHERE id = ?",
            (lead_id, now(), sid),
        )
        notify(conn, lead_id, "placement", "You are now the lead for a project.",
               f"/o/{org_id}/projects/{sid}")  # fmt: skip


def published_state(conn: sqlite3.Connection, project_ids: list[str]) -> dict[str, dict]:
    """SQL freshness check for graph results: which projects are published, at which revision."""
    if not project_ids:
        return {}
    marks = ",".join("?" * len(project_ids))
    return {
        r["id"]: dict(r)
        for r in conn.execute(
            f"SELECT id, org_id, lead_id, approved_revision, version FROM submissions "
            f"WHERE id IN ({marks}) AND state = 'published'",
            project_ids,
        )
    }
