"""Each organisation's own onboarding workflow: its steps, its email templates, and the
volunteers going through it.

An organisation's admins (and system admins) edit the workflow. A volunteer who joins an
organisation whose workflow is switched on gets a *run*: they work through the steps in order.
Each step is ticked off either by the volunteer or by an org admin, as the step says, and
reaching a step sends the volunteer that step's email, if it has one. Leaving the organisation
cancels the run; joining again starts a new one.

A project can have a workflow of its own, for volunteers joining its team (every function here
takes an optional `project_id`). It works the same way inside the organisation's rules:

* the project's lead, whom the org admins assign, and the org admins themselves edit it and
  confirm its steps; nobody else can;
* it uses the organisation's email templates, which only org admins write;
* a run starts when an invited volunteer says yes (app.placements) and never before they have
  finished the organisation's own onboarding; finishing the steps does not put anyone on the
  team: the lead side then accepts them.

This is separate from account vetting (app.services.vetting), which a moderator does once for the
whole network, and it does not decide who is matched: the graph still does that.

Every edit carries the workflow's `version` and bumps it, so two admins editing at once cannot
silently overwrite each other.
"""

import hashlib
import sqlite3

from app import mailer
from app.notify import notify
from app.services import teams
from app.services.access import check_access, get_org
from app.services.errors import TITLE_MAX, Conflict, Forbidden, Invalid, NotFound
from app.sqlstore import Database, enqueue, new_id, now

MAX_STEPS = 20
MAX_TEMPLATES = 20
DESCRIPTION_MAX = 1000
NAME_MAX = 60
COMPLETED_BY = {"volunteer": "The volunteer", "org_admin": "An organisation admin"}
PROJECT_COMPLETED_BY = {"volunteer": "The volunteer", "lead": "The project lead or an org admin"}
ALL_COMPLETED_BY = COMPLETED_BY | PROJECT_COMPLETED_BY
AWAITING_ACCEPTANCE = "Steps done: waiting to be accepted onto the team"
RUN_STATES = {"in_progress": "In progress", "completed": "Completed", "cancelled": "Cancelled"}
PAGE = "/onboarding"

# What a new organisation starts with: switched off, and theirs to change.
STARTER_TEMPLATES = (
    (
        "Welcome",
        "Welcome to {{ organisation_name }}",
        "Hello {{ volunteer_name }},\n\n"
        "Thank you for joining {{ organisation_name }} as a volunteer. There are "
        "{{ steps_total }} short steps before you start. The first is:\n\n"
        "{{ step_title }}\n{{ step_description }}\n\n"
        "You can follow your progress here: {{ onboarding_url }}\n\n"
        "{{ organisation_name }}",
    ),
    (
        "Next step",
        "{{ organisation_name }}: your next step",
        "Hello {{ volunteer_name }},\n\n"
        "You have finished {{ steps_done }} of {{ steps_total }} onboarding steps. Next:\n\n"
        "{{ step_title }}\n{{ step_description }}\n\n"
        "{{ onboarding_url }}\n\n"
        "{{ organisation_name }}",
    ),
    (
        "Onboarding complete",
        "You are all set with {{ organisation_name }}",
        "Hello {{ volunteer_name }},\n\n"
        "You have finished onboarding with {{ organisation_name }}. Thank you. We will be in "
        "touch about projects that fit your skills.\n\n"
        "{{ organisation_name }}",
    ),
)
STARTER_STEPS = (  # (title, description, completed_by, template name)
    (
        "Read our code of conduct",
        "Read how we work together and confirm that you agree to it.",
        "volunteer",
        "Welcome",
    ),
    (
        "Welcome call",
        "A short call with one of our coordinators. We will get in touch to arrange it.",
        "org_admin",
        "Next step",
    ),
)


def completers(project_id: str = "") -> dict[str, str]:
    """Who may be named as completing a step of this kind of workflow."""
    return PROJECT_COMPLETED_BY if project_id else COMPLETED_BY


def _project(conn: sqlite3.Connection, org_id: str, project_id: str = "") -> None:
    """Queue the workflow for the graph, in the caller's transaction: the steps and where each
    volunteer is are held in Relate too, where the assistants read them. A project's workflow
    is reconciled together with the project itself."""
    if project_id:
        enqueue(conn, "project", project_id)
    else:
        enqueue(conn, "onboarding", org_id)


def _authorise(conn: sqlite3.Connection, actor_id: str, org_id: str, project_id: str = "",
               volunteer_id: str = "") -> None:  # fmt: skip
    """Who runs a workflow: the organisation's admins for its own, and the lead side (the lead
    the org admins assigned, or an org admin) for one of its projects."""
    if not project_id:
        check_access(conn, actor_id, org_id, role="org_admin")
        return
    get_org(conn, org_id)
    info = teams.check_lead_side(conn, actor_id, project_id, volunteer_id)
    if info["org_id"] != org_id:
        raise NotFound("project")


def _starter_id(prefix: str, org_id: str, name: str) -> str:
    """Stable for an organisation, so reseeding the demo rebuilds the same graph."""
    return f"{prefix}_{hashlib.sha256(f'{org_id}:{name}'.encode()).hexdigest()[:16]}"


# ------------------------------------------------------------------ the workflow (org admins)
def install_starter(conn: sqlite3.Connection, org_id: str) -> None:
    """Give a new organisation the starter workflow, switched off."""
    stamp = now()
    ids = {}
    for name, subject, body in STARTER_TEMPLATES:
        ids[name] = _starter_id("tpl", org_id, name)
        conn.execute(
            "INSERT INTO email_templates (id, org_id, name, subject, body, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ids[name], org_id, name, subject, body, stamp),
        )
    for position, (title, description, who, template) in enumerate(STARTER_STEPS, start=1):
        conn.execute(
            "INSERT INTO onboarding_steps (id, org_id, position, title, description, "
            "completed_by, email_template_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_starter_id("step", org_id, title), org_id, position, title, description, who,
             ids[template]),
        )  # fmt: skip
    conn.execute(
        "INSERT INTO onboarding_workflows (org_id, enabled, completion_template_id, updated_at) "
        "VALUES (?, 0, ?, ?)",
        (org_id, ids["Onboarding complete"], stamp),
    )
    _project(conn, org_id)


def install_project_flow(
    conn: sqlite3.Connection, org_id: str, project_id: str, steps: list[dict], *, enabled: bool
) -> None:
    """Give a project a ready-made team onboarding (the demo seed). Each step is a dict with
    `title`, `description`, `completed_by` and optionally `template`, the name of one of the
    organisation's starter templates. IDs are stable, like the starter workflow's."""
    for position, step in enumerate(steps, start=1):
        template = step.get("template")
        conn.execute(
            "INSERT INTO onboarding_steps (id, org_id, project_id, position, title, "
            "description, completed_by, email_template_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (_starter_id("step", project_id, step["title"]), org_id, project_id, position,
             *_step_fields(step["title"], step.get("description", ""), step["completed_by"],
                           project_id),
             _starter_id("tpl", org_id, template) if template else None),
        )  # fmt: skip
    conn.execute(
        "INSERT INTO onboarding_workflows (org_id, project_id, enabled, updated_at) "
        "VALUES (?, ?, ?, ?)",
        (org_id, project_id, int(enabled and bool(steps)), now()),
    )
    _project(conn, org_id, project_id)


def _settings(conn: sqlite3.Connection, org_id: str, project_id: str = "") -> dict:
    row = conn.execute(
        "SELECT enabled, completion_template_id, version, updated_at "
        "FROM onboarding_workflows WHERE org_id = ? AND project_id = ?",
        (org_id, project_id),
    ).fetchone()
    if row is None:  # nothing set up yet (every project starts here): empty and switched off
        return {"enabled": False, "completion_template_id": None, "version": 0, "updated_at": None}
    return dict(row) | {"enabled": bool(row["enabled"])}


def _steps(conn: sqlite3.Connection, org_id: str, project_id: str = "") -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT id, position, title, description, completed_by, email_template_id "
            "FROM onboarding_steps WHERE org_id = ? AND project_id = ? ORDER BY position, id",
            (org_id, project_id),
        )
    ]


def _templates(conn: sqlite3.Connection, org_id: str) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT id, name, subject, body, updated_at FROM email_templates "
            "WHERE org_id = ? ORDER BY name, id",
            (org_id,),
        )
    ]


def load_workflow(
    conn: sqlite3.Connection, actor_id: str, org_id: str, project_id: str = ""
) -> dict:
    """Everything an editor sees: the switch, the steps in order and the organisation's email
    templates (a project's workflow chooses from them; only org admins change them)."""
    _authorise(conn, actor_id, org_id, project_id)
    return {
        **_settings(conn, org_id, project_id),
        "steps": _steps(conn, org_id, project_id),
        "completers": [
            {"value": value, "label": label} for value, label in completers(project_id).items()
        ],
        "templates": _templates(conn, org_id),
        "placeholders": [
            {"name": name, "meaning": meaning} for name, meaning in mailer.PLACEHOLDERS.items()
        ],
    }


def get_workflow(db: Database, actor_id: str, org_id: str, project_id: str = "") -> dict:
    with db.read() as conn:
        return load_workflow(conn, actor_id, org_id, project_id)


def _edit(conn: sqlite3.Connection, actor_id: str, org_id: str, expected_version: int,
          project_id: str = "") -> None:  # fmt: skip
    """Start an edit: the actor must run this workflow and must have seen its current version."""
    _authorise(conn, actor_id, org_id, project_id)
    stamp = now()
    conn.execute(
        "INSERT OR IGNORE INTO onboarding_workflows (org_id, project_id, version, updated_at) "
        "VALUES (?, ?, 0, ?)",
        (org_id, project_id, stamp),
    )
    changed = conn.execute(
        "UPDATE onboarding_workflows SET version = version + 1, updated_at = ?, updated_by = ? "
        "WHERE org_id = ? AND project_id = ? AND version = ?",
        (stamp, actor_id, org_id, project_id, expected_version),
    ).rowcount
    if not changed:
        raise Conflict("The workflow changed since you opened it. Reload and try again.")
    _project(conn, org_id, project_id)  # commits with whatever the edit goes on to change


def _own_template(conn: sqlite3.Connection, org_id: str, template_id: str | None) -> str | None:
    """A template ID that belongs to this organisation, or None for 'no email'."""
    if not template_id:
        return None
    if conn.execute(
        "SELECT 1 FROM email_templates WHERE id = ? AND org_id = ?", (template_id, org_id)
    ).fetchone() is None:  # fmt: skip
        raise Invalid("Choose one of this organisation's email templates.")
    return template_id


def update_workflow(
    db: Database, actor_id: str, org_id: str, *, enabled: bool,
    completion_template_id: str | None, expected_version: int, project_id: str = "",
) -> None:  # fmt: skip
    """Switch the workflow on or off and choose the email sent when a volunteer finishes.

    Switching on affects volunteers who join from now on (for a project: who accept an
    invitation from now on); current members are not enrolled. Switching off leaves the runs
    already started as they are."""
    with db.tx() as conn:
        _edit(conn, actor_id, org_id, expected_version, project_id)
        if enabled and not _steps(conn, org_id, project_id):
            raise Invalid("Add at least one step before switching the workflow on.")
        conn.execute(
            "UPDATE onboarding_workflows SET enabled = ?, completion_template_id = ? "
            "WHERE org_id = ? AND project_id = ?",
            (int(enabled), _own_template(conn, org_id, completion_template_id), org_id,
             project_id),
        )  # fmt: skip


def _step_fields(
    title: str, description: str, completed_by: str, project_id: str = ""
) -> tuple[str, str, str]:
    title, description = " ".join(title.split()), description.strip()
    if not 3 <= len(title) <= TITLE_MAX:
        raise Invalid(f"Give the step a title (3–{TITLE_MAX} characters).")
    if len(description) > DESCRIPTION_MAX:
        raise Invalid(f"Keep the description under {DESCRIPTION_MAX} characters.")
    if completed_by not in completers(project_id):
        other = "the project lead" if project_id else "an organisation admin"
        raise Invalid(f"A step is completed by the volunteer or by {other}.")
    return title, description, completed_by


def add_step(
    db: Database, actor_id: str, org_id: str, *, title: str, description: str = "",
    completed_by: str, email_template_id: str | None = None, expected_version: int,
    project_id: str = "",
) -> str:  # fmt: skip
    """Add a step at the end of the workflow. Returns its ID."""
    fields = _step_fields(title, description, completed_by, project_id)
    step_id = new_id("step")
    with db.tx() as conn:
        _edit(conn, actor_id, org_id, expected_version, project_id)
        count, last = conn.execute(
            "SELECT count(*), COALESCE(max(position), 0) FROM onboarding_steps "
            "WHERE org_id = ? AND project_id = ?",
            (org_id, project_id),
        ).fetchone()
        if count >= MAX_STEPS:
            raise Invalid(f"A workflow has at most {MAX_STEPS} steps.")
        conn.execute(
            "INSERT INTO onboarding_steps (id, org_id, project_id, position, title, "
            "description, completed_by, email_template_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (step_id, org_id, project_id, last + 1, *fields,
             _own_template(conn, org_id, email_template_id)),
        )  # fmt: skip
        # Volunteers who had finished every step so far stay finished; nobody is reopened.
    return step_id


def _require_step(
    conn: sqlite3.Connection, org_id: str, step_id: str, project_id: str = ""
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, position, title, description, completed_by, email_template_id "
        "FROM onboarding_steps WHERE id = ? AND org_id = ? AND project_id = ?",
        (step_id, org_id, project_id),
    ).fetchone()
    if row is None:
        raise NotFound("step")
    return row


def update_step(
    db: Database, actor_id: str, org_id: str, step_id: str, *, title: str, description: str = "",
    completed_by: str, email_template_id: str | None = None, expected_version: int,
    project_id: str = "",
) -> None:  # fmt: skip
    fields = _step_fields(title, description, completed_by, project_id)
    with db.tx() as conn:
        _edit(conn, actor_id, org_id, expected_version, project_id)
        _require_step(conn, org_id, step_id, project_id)
        conn.execute(
            "UPDATE onboarding_steps SET title = ?, description = ?, completed_by = ?, "
            "email_template_id = ? WHERE id = ?",
            (*fields, _own_template(conn, org_id, email_template_id), step_id),
        )


def move_step(
    db: Database, actor_id: str, org_id: str, step_id: str, *, direction: str,
    expected_version: int, project_id: str = "",
) -> None:  # fmt: skip
    """Swap a step with the one before ('up') or after ('down') it."""
    if direction not in ("up", "down"):
        raise Invalid("Move a step up or down.")
    with db.tx() as conn:
        _edit(conn, actor_id, org_id, expected_version, project_id)
        _require_step(conn, org_id, step_id, project_id)
        order = [s["id"] for s in _steps(conn, org_id, project_id)]
        at = order.index(step_id)
        to = at - 1 if direction == "up" else at + 1
        if not 0 <= to < len(order):
            raise Invalid(f"That step is already {'first' if direction == 'up' else 'last'}.")
        order[at], order[to] = order[to], order[at]
        _renumber(conn, order)
        _advance_all(conn, org_id, project_id)


def _renumber(conn: sqlite3.Connection, order: list[str]) -> None:
    conn.executemany(
        "UPDATE onboarding_steps SET position = ? WHERE id = ?",
        [(position, step_id) for position, step_id in enumerate(order, start=1)],
    )


def delete_step(
    db: Database, actor_id: str, org_id: str, step_id: str, *, expected_version: int,
    project_id: str = "",
) -> None:  # fmt: skip
    """Remove a step. Volunteers who were waiting on it move on to the next one."""
    with db.tx() as conn:
        _edit(conn, actor_id, org_id, expected_version, project_id)
        _require_step(conn, org_id, step_id, project_id)
        conn.execute("DELETE FROM onboarding_steps WHERE id = ?", (step_id,))
        remaining = [s["id"] for s in _steps(conn, org_id, project_id)]
        _renumber(conn, remaining)
        if not remaining:  # nothing left to go through
            conn.execute(
                "UPDATE onboarding_workflows SET enabled = 0 WHERE org_id = ? AND project_id = ?",
                (org_id, project_id),
            )
        _advance_all(conn, org_id, project_id)


# ------------------------------------------------------------------ email templates (org admins)
def _template_fields(name: str, subject: str, body: str) -> tuple[str, str, str]:
    name = " ".join(name.split())
    subject, body = " ".join(subject.split()), body.strip().replace("\r\n", "\n")
    if not 2 <= len(name) <= NAME_MAX:
        raise Invalid(f"Give the template a name (2–{NAME_MAX} characters).")
    if not 3 <= len(subject) <= mailer.SUBJECT_MAX:
        raise Invalid(f"Give the email a subject (3–{mailer.SUBJECT_MAX} characters).")
    if not 10 <= len(body) <= mailer.BODY_MAX:
        raise Invalid(f"Write the email's text (10–{mailer.BODY_MAX} characters).")
    if problem := mailer.template_problems(subject, body):
        raise Invalid(problem)
    return name, subject, body


def save_template(
    db: Database, actor_id: str, org_id: str, template_id: str | None = None, *, name: str,
    subject: str, body: str, expected_version: int,
) -> str:  # fmt: skip
    """Create a template (no `template_id`) or replace one. Returns its ID."""
    fields = _template_fields(name, subject, body)
    try:
        with db.tx() as conn:
            _edit(conn, actor_id, org_id, expected_version)
            if template_id is None:
                count = conn.execute(
                    "SELECT count(*) FROM email_templates WHERE org_id = ?", (org_id,)
                ).fetchone()[0]
                if count >= MAX_TEMPLATES:
                    raise Invalid(f"An organisation has at most {MAX_TEMPLATES} templates.")
                template_id = new_id("tpl")
                conn.execute(
                    "INSERT INTO email_templates (id, org_id, name, subject, body, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (template_id, org_id, *fields, now()),
                )
            elif not conn.execute(
                "UPDATE email_templates SET name = ?, subject = ?, body = ?, updated_at = ? "
                "WHERE id = ? AND org_id = ?",
                (*fields, now(), template_id, org_id),
            ).rowcount:
                raise NotFound("template")
    except sqlite3.IntegrityError as error:
        raise Conflict("This organisation already has a template with that name.") from error
    return template_id


def delete_template(
    db: Database, actor_id: str, org_id: str, template_id: str, *, expected_version: int
) -> None:
    """Delete a template. Steps that used it send no email from then on."""
    with db.tx() as conn:
        _edit(conn, actor_id, org_id, expected_version)
        if not conn.execute(
            "DELETE FROM email_templates WHERE id = ? AND org_id = ?", (template_id, org_id)
        ).rowcount:
            raise NotFound("template")


def preview_template(
    db: Database, actor_id: str, org_id: str, *, subject: str, body: str, public_url: str
) -> dict:
    """The email as a volunteer would receive it, filled in with sample values. Nothing is saved
    or sent, so an admin can try a wording before keeping it."""
    _name, subject, body = _template_fields("Preview", subject, body)
    with db.read() as conn:
        check_access(conn, actor_id, org_id, role="org_admin")
        org = get_org(conn, org_id)
    values = mailer.SAMPLE | {
        "organisation_name": org["name"],
        "onboarding_url": public_url.rstrip("/") + PAGE,
    }
    return {"subject": mailer.render(subject, values), "body": mailer.render(body, values)}


def send_test(db: Database, actor_id: str, org_id: str, template_id: str) -> None:
    """Queue the template, filled in with sample values, to the admin's own address."""
    with db.tx() as conn:
        check_access(conn, actor_id, org_id, role="org_admin")
        org = get_org(conn, org_id)
        template = conn.execute(
            "SELECT name, subject, body FROM email_templates WHERE id = ? AND org_id = ?",
            (template_id, org_id),
        ).fetchone()
        if template is None:
            raise NotFound("template")
        mailer.queue(
            conn, org_id=org_id, user_id=actor_id, purpose="test", template=template,
            values=mailer.SAMPLE | {"organisation_name": org["name"]},
        )  # fmt: skip


def sent_emails(db: Database, actor_id: str, org_id: str, *, limit: int = 100) -> list[dict]:
    """The organisation's recent emails and whether they went out. Names, never addresses."""
    with db.read() as conn:
        check_access(conn, actor_id, org_id, role="org_admin")
        return [
            dict(r)
            for r in conn.execute(
                "SELECT e.id, u.display_name AS recipient, e.purpose, e.template_name, "
                "e.subject, e.status, e.attempts, e.created_at, e.sent_at "
                "FROM email_outbox e JOIN users u ON u.id = e.user_id "
                "WHERE e.org_id = ? ORDER BY e.id DESC LIMIT ?",
                (org_id, max(1, min(limit, 500))),
            )
        ]


# ------------------------------------------------------------------ runs
def enabled(conn: sqlite3.Connection, org_id: str, project_id: str = "") -> bool:
    return _settings(conn, org_id, project_id)["enabled"]


def pending_with_org(conn: sqlite3.Connection, user_id: str, org_id: str) -> bool:
    """Whether the volunteer is still part-way through the organisation's own onboarding. While
    they are, no project of the organisation takes them onto its team."""
    return (
        conn.execute(
            "SELECT 1 FROM onboarding_runs WHERE user_id = ? AND org_id = ? AND project_id = '' "
            "AND state = 'in_progress'",
            (user_id, org_id),
        ).fetchone()
        is not None
    )


def steps_left(conn: sqlite3.Connection, user_id: str, org_id: str, project_id: str) -> int:
    """How many of the workflow's steps the volunteer has still to finish (0 with no run)."""
    run = conn.execute(
        "SELECT state FROM onboarding_runs WHERE user_id = ? AND org_id = ? AND project_id = ?",
        (user_id, org_id, project_id),
    ).fetchone()
    if run is None or run["state"] != "in_progress":
        return 0
    return conn.execute(
        "SELECT count(*) FROM onboarding_steps s WHERE s.org_id = ? AND s.project_id = ? "
        "AND NOT EXISTS (SELECT 1 FROM onboarding_progress p WHERE p.step_id = s.id "
        "                AND p.user_id = ? AND p.state = 'done')",
        (org_id, project_id, user_id),
    ).fetchone()[0]


def start(conn: sqlite3.Connection, user_id: str, org_id: str, project_id: str = "") -> bool:
    """A volunteer has just joined the organisation, or said yes to a project's invitation:
    start their run, if the workflow is switched on. Returns whether one started."""
    if not enabled(conn, org_id, project_id):
        return False
    conn.execute(
        "DELETE FROM onboarding_progress WHERE user_id = ? AND step_id IN "
        "(SELECT id FROM onboarding_steps WHERE org_id = ? AND project_id = ?)",
        (user_id, org_id, project_id),
    )
    conn.execute(
        "INSERT INTO onboarding_runs (user_id, org_id, project_id, state, started_at) "
        "VALUES (?, ?, ?, 'in_progress', ?) ON CONFLICT (user_id, org_id, project_id) "
        "DO UPDATE SET state = 'in_progress', started_at = excluded.started_at, "
        "completed_at = NULL",
        (user_id, org_id, project_id, now()),
    )
    _advance(conn, user_id, org_id, project_id)
    _project(conn, org_id, project_id)
    return True


def cancel(conn: sqlite3.Connection, user_id: str, org_id: str) -> None:
    """The volunteer left the organisation or was removed: an unfinished run ends there. (Their
    runs with its projects end with their places on those teams: app.services.teams.)"""
    if conn.execute(
        "UPDATE onboarding_runs SET state = 'cancelled' "
        "WHERE user_id = ? AND org_id = ? AND project_id = '' AND state = 'in_progress'",
        (user_id, org_id),
    ).rowcount:
        _project(conn, org_id)


def _advance_all(conn: sqlite3.Connection, org_id: str, project_id: str = "") -> None:
    """After the steps changed: put every volunteer still onboarding on their right step."""
    for (user_id,) in conn.execute(
        "SELECT user_id FROM onboarding_runs WHERE org_id = ? AND project_id = ? "
        "AND state = 'in_progress' ORDER BY started_at, user_id",
        (org_id, project_id),
    ).fetchall():
        _advance(conn, user_id, org_id, project_id)


def _advance(conn: sqlite3.Connection, user_id: str, org_id: str, project_id: str = "") -> None:
    """Open the volunteer's next step, telling them once, or finish the run when none is left."""
    steps = _steps(conn, org_id, project_id)
    progress = dict(
        conn.execute(
            "SELECT p.step_id, p.state FROM onboarding_progress p "
            "JOIN onboarding_steps s ON s.id = p.step_id "
            "WHERE p.user_id = ? AND s.org_id = ? AND s.project_id = ?",
            (user_id, org_id, project_id),
        ).fetchall()
    )
    done = sum(1 for s in steps if progress.get(s["id"]) == "done")
    current = next((s for s in steps if progress.get(s["id"]) != "done"), None)
    org = get_org(conn, org_id)
    project = teams.project_info(conn, project_id) if project_id else None
    # What the volunteer is onboarding with, and who confirms a step that is not theirs.
    what = f"“{project['title']}”" if project else org["name"]
    confirmer = "The project lead" if project else org["name"]
    name = conn.execute("SELECT display_name FROM users WHERE id = ?", (user_id,)).fetchone()[0]
    values = {
        "volunteer_name": name,
        "organisation_name": org["name"],
        "project_title": project["title"] if project else "",
        "steps_done": str(done),
        "steps_total": str(len(steps)),
    }
    stamp = now()
    if current is None:
        conn.execute(
            "UPDATE onboarding_runs SET state = 'completed', completed_at = ? "
            "WHERE user_id = ? AND org_id = ? AND project_id = ?",
            (stamp, user_id, org_id, project_id),
        )
        if project:
            # Finishing the steps puts nobody on a team: the lead side accepts them.
            notify(conn, user_id, "onboarding",
                   f"You have finished the onboarding steps for {what}. Its lead will now "
                   "confirm your place on the team.", PAGE)  # fmt: skip
            teams.notify_deciders(
                conn, org_id, project["lead_id"],
                f"{name} has finished the onboarding steps for {what}. Accept them onto the "
                "team when you are ready.",
                teams.project_link(org_id, project_id), exclude=(user_id,),
            )  # fmt: skip
        else:
            notify(conn, user_id, "onboarding",
                   f"You have finished onboarding with {org['name']}.", PAGE)  # fmt: skip
        _send(conn, org_id, user_id, "completed",
              _settings(conn, org_id, project_id)["completion_template_id"], values)  # fmt: skip
        return
    # Steps after the current one are not open yet, even if a reorder put an opened one there.
    conn.execute(
        "DELETE FROM onboarding_progress WHERE user_id = ? AND state = 'open' AND step_id != ? "
        "AND step_id IN (SELECT id FROM onboarding_steps WHERE org_id = ? AND project_id = ?)",
        (user_id, current["id"], org_id, project_id),
    )
    if current["id"] in progress:
        return  # already their current step: they have been told
    conn.execute(
        "INSERT INTO onboarding_progress (user_id, step_id, state, opened_at) "
        "VALUES (?, ?, 'open', ?)",
        (user_id, current["id"], stamp),
    )
    waiting = "" if current["completed_by"] == "volunteer" else f" {confirmer} will confirm it."
    notify(conn, user_id, "onboarding",
           f"{what} onboarding, step {done + 1} of {len(steps)}: {current['title']}."
           f"{waiting}", PAGE)  # fmt: skip
    _send(conn, org_id, user_id, "step", current["email_template_id"], values | {
        "step_title": current["title"], "step_description": current["description"],
    })  # fmt: skip


def _send(conn, org_id: str, user_id: str, purpose: str, template_id: str | None, values) -> None:
    if not template_id:
        return
    template = conn.execute(
        "SELECT name, subject, body FROM email_templates WHERE id = ? AND org_id = ?",
        (template_id, org_id),
    ).fetchone()
    if template is not None:
        mailer.queue(
            conn, org_id=org_id, user_id=user_id, purpose=purpose, template=template,
            values=values,
        )  # fmt: skip


def complete_step(
    db: Database, actor_id: str, org_id: str, step_id: str, *, user_id: str | None = None,
    project_id: str = "",
) -> dict:  # fmt: skip
    """Tick off a volunteer's current step. Without `user_id` the actor completes their own; with
    it, whoever runs the workflow completes it for that volunteer (any step, including the
    volunteer's own): an org admin, or for a project's workflow its lead side.
    Returns the run as the volunteer sees it."""
    volunteer = user_id or actor_id
    with db.tx() as conn:
        if user_id is not None:
            _authorise(conn, actor_id, org_id, project_id, volunteer_id=user_id)
        else:
            get_org(conn, org_id)
        step = _require_step(conn, org_id, step_id, project_id)
        run = conn.execute(
            "SELECT state FROM onboarding_runs WHERE user_id = ? AND org_id = ? AND project_id = ?",
            (volunteer, org_id, project_id),
        ).fetchone()
        if run is None:
            raise NotFound("onboarding")
        if user_id is None and step["completed_by"] != "volunteer":
            raise Forbidden(
                "Only the project lead or an org admin can confirm that step."
                if project_id
                else "org_admin"
            )
        changed = 0
        if run["state"] == "in_progress":
            changed = conn.execute(
                "UPDATE onboarding_progress SET state = 'done', done_at = ?, done_by = ? "
                "WHERE user_id = ? AND step_id = ? AND state = 'open'",
                (now(), actor_id, volunteer, step_id),
            ).rowcount
        if not changed:
            raise Conflict("That is not the current step. Reload to see where things stand.")
        _advance(conn, volunteer, org_id, project_id)
        _project(conn, org_id, project_id)
        return _run(conn, volunteer, org_id, project_id)


def _run(conn: sqlite3.Connection, user_id: str, org_id: str, project_id: str = "") -> dict:
    run = conn.execute(
        "SELECT r.org_id, o.name AS organisation, r.project_id, "
        "COALESCE(s.title, '') AS project, r.state, r.started_at, r.completed_at, "
        "pl.state AS team_state "
        "FROM onboarding_runs r JOIN organisations o ON o.id = r.org_id "
        "LEFT JOIN submission_current s ON s.id = r.project_id "
        "LEFT JOIN placements pl ON pl.project_id = r.project_id AND pl.volunteer_id = r.user_id "
        "WHERE r.user_id = ? AND r.org_id = ? AND r.project_id = ?",
        (user_id, org_id, project_id),
    ).fetchone()
    steps = [
        dict(r)
        for r in conn.execute(
            "SELECT s.id, s.position, s.title, s.description, s.completed_by, "
            "COALESCE(p.state, 'waiting') AS state, p.done_at "
            "FROM onboarding_steps s LEFT JOIN onboarding_progress p "
            "  ON p.step_id = s.id AND p.user_id = ? WHERE s.org_id = ? AND s.project_id = ? "
            "ORDER BY s.position, s.id",
            (user_id, org_id, project_id),
        )
    ]
    for step in steps:
        step["can_complete"] = (
            run["state"] == "in_progress"
            and step["state"] == "open"
            and step["completed_by"] == "volunteer"
        )
    awaiting = run["state"] == "completed" and run["team_state"] == "onboarding"
    return {
        **dict(run),
        "state_label": AWAITING_ACCEPTANCE if awaiting else RUN_STATES[run["state"]],
        "awaiting_acceptance": awaiting,
        "steps_done": sum(1 for s in steps if s["state"] == "done"),
        "steps_total": len(steps),
        "steps": steps,
    }


def my_onboarding(db: Database, user_id: str) -> list[dict]:
    """The volunteer's own runs, unfinished first: each organisation's steps, then those of its
    projects whose teams they are joining, and where they are in each."""
    with db.read() as conn:
        runs = conn.execute(
            "SELECT r.org_id, r.project_id FROM onboarding_runs r "
            "JOIN organisations o ON o.id = r.org_id "
            "WHERE r.user_id = ? AND r.state != 'cancelled' "
            "ORDER BY r.state = 'completed', o.name, r.project_id",
            (user_id,),
        ).fetchall()
        return [_run(conn, user_id, row["org_id"], row["project_id"]) for row in runs]


def open_count(conn: sqlite3.Connection, user_id: str) -> int:
    return conn.execute(
        "SELECT count(*) FROM onboarding_runs WHERE user_id = ? AND state = 'in_progress'",
        (user_id,),
    ).fetchone()[0]


def org_progress(db: Database, actor_id: str, org_id: str, project_id: str = "") -> list[dict]:
    with db.read() as conn:
        return load_progress(conn, actor_id, org_id, project_id)


def load_progress(
    conn: sqlite3.Connection, actor_id: str, org_id: str, project_id: str = ""
) -> list[dict]:
    """Where each volunteer is in the workflow, for whoever runs it (no emails). For a project,
    `awaiting_acceptance` marks the volunteers who finished and wait for the lead side."""
    _authorise(conn, actor_id, org_id, project_id)
    total = conn.execute(
        "SELECT count(*) FROM onboarding_steps WHERE org_id = ? AND project_id = ?",
        (org_id, project_id),
    ).fetchone()[0]
    rows = conn.execute(
        "SELECT r.user_id, u.display_name, r.state, r.started_at, r.completed_at, "
        "(SELECT count(*) FROM onboarding_progress p JOIN onboarding_steps s "
        "   ON s.id = p.step_id WHERE p.user_id = r.user_id AND s.org_id = r.org_id "
        "   AND s.project_id = r.project_id AND p.state = 'done') AS steps_done, "
        "c.id AS current_step_id, c.title AS current_step, c.completed_by AS waiting_for, "
        "(r.state = 'completed' AND COALESCE(pl.state, '') = 'onboarding') "
        "  AS awaiting_acceptance "
        "FROM onboarding_runs r JOIN users u ON u.id = r.user_id "
        "LEFT JOIN onboarding_steps c ON r.state = 'in_progress' AND c.org_id = r.org_id "
        "  AND c.project_id = r.project_id "
        "  AND c.id IN (SELECT p.step_id FROM onboarding_progress p "
        "               WHERE p.user_id = r.user_id AND p.state = 'open') "
        "LEFT JOIN placements pl ON pl.project_id = r.project_id "
        "  AND pl.volunteer_id = r.user_id "
        "WHERE r.org_id = ? AND r.project_id = ? AND r.state != 'cancelled' "
        "ORDER BY r.state = 'completed', r.started_at, u.display_name, u.id",
        (org_id, project_id),
    ).fetchall()
    return [
        dict(r)
        | {
            "awaiting_acceptance": bool(r["awaiting_acceptance"]),
            "state_label": AWAITING_ACCEPTANCE
            if r["awaiting_acceptance"]
            else RUN_STATES[r["state"]],
            "steps_total": total,
        }
        for r in rows
    ]
