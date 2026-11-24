"""SQLite: the transactional, authoritative store.

Holds identities, sessions, organisation membership and roles, profile edits, submissions,
review history and the projection outbox. None of the private columns here (email, password
hash, session tokens, private review notes) are ever written to the graph.
"""

import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,          -- normalised: stripped + lower-cased
    display_name  TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    profile_version INTEGER NOT NULL DEFAULT 0,
    -- Onboarding: draft -> screening -> active (or info_requested / declined / suspended)
    status        TEXT NOT NULL DEFAULT 'draft' CHECK (status IN (
                      'draft', 'screening', 'info_requested', 'active', 'declined', 'suspended')),
    status_version INTEGER NOT NULL DEFAULT 0,
    vetted_by     TEXT REFERENCES users(id),
    vetted_at     TEXT,
    created_at    TEXT NOT NULL
);

-- System-wide roles: admin (all access) and moderator (vets volunteers). Never from a form.
CREATE TABLE IF NOT EXISTS system_roles (
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role       TEXT NOT NULL CHECK (role IN ('admin', 'moderator')),
    granted_at TEXT NOT NULL,
    granted_by TEXT,
    PRIMARY KEY (user_id, role)
);

CREATE TABLE IF NOT EXISTS vetting_events (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id              TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    actor_id             TEXT NOT NULL REFERENCES users(id),
    from_status          TEXT NOT NULL,
    to_status            TEXT NOT NULL,
    note_for_volunteer   TEXT NOT NULL DEFAULT '',
    private_note         TEXT NOT NULL DEFAULT '',  -- moderators and admins only
    created_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS vetting_events_user ON vetting_events(user_id);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash    TEXT PRIMARY KEY,               -- sha256 of the opaque token; never the token
    user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL CHECK (kind IN ('cookie', 'bearer')),
    csrf_token    TEXT NOT NULL,
    active_org_id TEXT REFERENCES organisations(id),
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    revoked_at    TEXT
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id, created_at);

CREATE TABLE IF NOT EXISTS organisations (
    id          TEXT PRIMARY KEY,
    key         TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT ''
);

-- Volunteering is opt-in per organisation; opting out keeps the row with opted_in = 0.
CREATE TABLE IF NOT EXISTS memberships (
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    org_id     TEXT NOT NULL REFERENCES organisations(id),
    opted_in   INTEGER NOT NULL CHECK (opted_in IN (0, 1)),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, org_id)
);
CREATE INDEX IF NOT EXISTS memberships_org ON memberships(org_id, opted_in);

-- Organisation roles: org_admin (manages the organisation's projects and volunteers, and holds
-- the other two roles), reviewer (reviews suggestions) and team_coordinator (assigns project
-- leads and decides who joins the teams). Granted by a system admin, the seed or the CLI only.
CREATE TABLE IF NOT EXISTS org_roles (
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    org_id     TEXT NOT NULL REFERENCES organisations(id),
    role       TEXT NOT NULL CHECK (role IN ('reviewer', 'team_coordinator', 'org_admin')),
    granted_at TEXT NOT NULL,
    PRIMARY KEY (user_id, org_id, role)
);
CREATE INDEX IF NOT EXISTS org_roles_org ON org_roles(org_id);

CREATE TABLE IF NOT EXISTS causes (id TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS skills (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS profile_skills (
    user_id  TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    skill_id TEXT NOT NULL REFERENCES skills(id),
    has      INTEGER NOT NULL CHECK (has IN (0, 1)),
    offers   INTEGER NOT NULL CHECK (offers IN (0, 1)),
    excluded INTEGER NOT NULL CHECK (excluded IN (0, 1)),
    CHECK (NOT (excluded AND offers)),
    PRIMARY KEY (user_id, skill_id)
);

CREATE TABLE IF NOT EXISTS profile_causes (
    user_id  TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    cause_id TEXT NOT NULL REFERENCES causes(id),
    stance   TEXT NOT NULL CHECK (stance IN ('prefer', 'exclude')),
    PRIMARY KEY (user_id, cause_id)
);

CREATE TABLE IF NOT EXISTS submissions (
    id                TEXT PRIMARY KEY,
    org_id            TEXT NOT NULL REFERENCES organisations(id),
    author_id         TEXT NOT NULL REFERENCES users(id),
    lead_id           TEXT REFERENCES users(id),        -- project lead; the author by default
    -- 'draft' is private to the author and edited in place until they submit it for review.
    state             TEXT NOT NULL CHECK (state IN (
                          'draft', 'submitted', 'changes_requested', 'rejected',
                          'approved', 'published', 'withdrawn')),
    current_revision  INTEGER NOT NULL,
    version           INTEGER NOT NULL DEFAULT 1,   -- bumped on every transition
    approved_revision INTEGER,
    approved_by       TEXT REFERENCES users(id),
    approved_at       TEXT,
    published_at      TEXT,
    withdrawn_by      TEXT REFERENCES users(id),
    withdrawn_at      TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS submissions_org_state ON submissions(org_id, state);
CREATE INDEX IF NOT EXISTS submissions_author ON submissions(author_id);

CREATE TABLE IF NOT EXISTS submission_revisions (
    submission_id TEXT NOT NULL REFERENCES submissions(id),
    revision      INTEGER NOT NULL,
    title         TEXT NOT NULL,
    summary       TEXT NOT NULL,
    cause_id      TEXT REFERENCES causes(id),      -- NULL only while the submission is a draft
    created_at    TEXT NOT NULL,
    PRIMARY KEY (submission_id, revision)
);

CREATE TABLE IF NOT EXISTS revision_skills (
    submission_id TEXT NOT NULL,
    revision      INTEGER NOT NULL,
    skill_id      TEXT NOT NULL REFERENCES skills(id),
    required      INTEGER NOT NULL CHECK (required IN (0, 1)),
    PRIMARY KEY (submission_id, revision, skill_id),
    FOREIGN KEY (submission_id, revision) REFERENCES submission_revisions
);

-- A submission with the title of its current revision: what every list and notification shows.
CREATE VIEW IF NOT EXISTS submission_current AS
SELECT s.id, s.org_id, s.author_id, s.lead_id, s.state, s.current_revision, s.version,
       s.updated_at, v.title
FROM submissions s
JOIN submission_revisions v ON v.submission_id = s.id AND v.revision = s.current_revision;

CREATE TABLE IF NOT EXISTS reviews (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id     TEXT NOT NULL REFERENCES submissions(id),
    revision          INTEGER NOT NULL,
    reviewer_id       TEXT NOT NULL REFERENCES users(id),
    action            TEXT NOT NULL CHECK (action IN (
                          'changes_requested', 'rejected', 'approved', 'withdrawn')),
    reason_for_author TEXT NOT NULL,
    private_note      TEXT NOT NULL DEFAULT '',     -- reviewers of the organisation only
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reviews_submission ON reviews(submission_id);

-- A volunteer and a project that Relate found eligible: a candidate, shown ranked to the project
-- lead. The lead side (the lead or an org admin) invites; the volunteer then says yes and joins
-- the team (state 'team', projected as (:Volunteer)-[:ON_TEAM]->(:Project)). An `eligible` row
-- with lead_said = 'yes' is an open invitation. A project with its own onboarding workflow puts
-- the volunteer in 'onboarding' first: they work through its steps and the lead side then
-- accepts them onto the team. The lead side can also suspend a team member ('suspended': off
-- the team until reinstated) or remove them.
CREATE TABLE IF NOT EXISTS placements (
    project_id        TEXT NOT NULL REFERENCES submissions(id),
    volunteer_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    state             TEXT NOT NULL CHECK (state IN (
                          'eligible', 'onboarding', 'team', 'suspended', 'declined', 'lapsed',
                          'removed', 'left')),
    volunteer_said    TEXT CHECK (volunteer_said IN ('yes', 'no')),
    volunteer_said_at TEXT,
    lead_said         TEXT CHECK (lead_said IN ('yes', 'no')),
    lead_said_at      TEXT,
    lead_actor_id     TEXT REFERENCES users(id) ON DELETE SET NULL,  -- who answered for the lead
    lead_note         TEXT NOT NULL DEFAULT '',     -- optional reason with the lead-side answer
    invited_at        TEXT,                          -- first invitation; never cleared
    reminded_at       TEXT,                          -- reminder sent for the open invitation
    matched_skills    TEXT NOT NULL DEFAULT '[]',   -- JSON list, from the Relate match
    optional_skills   TEXT NOT NULL DEFAULT '[]',   -- JSON list, from the Relate match
    preferred         INTEGER NOT NULL DEFAULT 0 CHECK (preferred IN (0, 1)),  -- prefers cause
    version           INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    PRIMARY KEY (project_id, volunteer_id)
);
CREATE INDEX IF NOT EXISTS placements_volunteer ON placements(volunteer_id, state);

-- Every change to a placement: who did it (NULL: the system), what, and any reason given.
CREATE TABLE IF NOT EXISTS placement_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   TEXT NOT NULL REFERENCES submissions(id),
    volunteer_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    actor_id     TEXT REFERENCES users(id) ON DELETE SET NULL,
    event        TEXT NOT NULL,
    note         TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS placement_events_pair ON placement_events(project_id, volunteer_id);

CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,
    message    TEXT NOT NULL,
    link       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    read_at    TEXT
);
CREATE INDEX IF NOT EXISTS notifications_user ON notifications(user_id, read_at);

-- The audit trail: who did what, to what, and whether it was allowed. RBAC changes are written
-- in the transaction that makes them; requests and assistant runs are written as they finish.
-- `detail` is a small JSON object and never holds an email, a password or free text from a form.
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    actor_id    TEXT,                             -- NULL: not signed in, or the system
    action      TEXT NOT NULL,                    -- e.g. role.grant, http.post, agent.run
    outcome     TEXT NOT NULL CHECK (outcome IN ('ok', 'denied', 'invalid', 'error')),
    target_kind TEXT NOT NULL DEFAULT '',
    target_id   TEXT NOT NULL DEFAULT '',
    org_id      TEXT,
    source      TEXT NOT NULL DEFAULT 'system' CHECK (source IN ('web', 'api', 'agent', 'system')),
    detail      TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS audit_log_created ON audit_log(created_at);
CREATE INDEX IF NOT EXISTS audit_log_actor ON audit_log(actor_id, created_at);
CREATE INDEX IF NOT EXISTS audit_log_action ON audit_log(action, outcome);

-- One row per assistant run (app.agents): who ran which agent, what it cost, how it ended.
-- `messages` is kept only while a run waits for the user's approval, so it can be resumed.
CREATE TABLE IF NOT EXISTS agent_runs (
    id            TEXT PRIMARY KEY,
    agent         TEXT NOT NULL,
    actor_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    org_id        TEXT,
    subject_id    TEXT NOT NULL DEFAULT '',       -- the project or submission it was about
    status        TEXT NOT NULL CHECK (status IN (
                      'running', 'awaiting_approval', 'completed', 'failed')),
    model         TEXT NOT NULL DEFAULT '',
    requests      INTEGER NOT NULL DEFAULT 0,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    tool_calls    INTEGER NOT NULL DEFAULT 0,
    tools         TEXT NOT NULL DEFAULT '[]',     -- JSON list of the tools it called
    error         TEXT NOT NULL DEFAULT '',
    output        TEXT,                           -- JSON of the validated output
    pending       TEXT NOT NULL DEFAULT '[]',     -- JSON: actions waiting for approval
    messages      TEXT,                           -- JSON message history, only while waiting
    trace_id      TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    finished_at   TEXT
);
CREATE INDEX IF NOT EXISTS agent_runs_actor ON agent_runs(actor_id, created_at);
CREATE INDEX IF NOT EXISTS agent_runs_agent ON agent_runs(agent, created_at);

-- Each organisation's own onboarding workflow (app.services.onboarding): ordered steps a new
-- volunteer goes through after joining, each optionally announced by one of the organisation's
-- email templates. Org admins edit all of it; `version` is bumped by every edit, so a stale
-- editor is refused. Switched off (enabled = 0), joining the organisation starts nothing.
--
-- A project of the organisation can have a workflow of its own, for volunteers joining its team.
-- Those rows carry the project's ID in `project_id`; the organisation's own carry ''. A project's
-- workflow is edited by the project's lead and the org admins, and uses the organisation's email
-- templates.
CREATE TABLE IF NOT EXISTS onboarding_workflows (
    org_id                 TEXT NOT NULL REFERENCES organisations(id),
    project_id             TEXT NOT NULL DEFAULT '',     -- '' or submissions.id
    enabled                INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    completion_template_id TEXT REFERENCES email_templates(id) ON DELETE SET NULL,
    version                INTEGER NOT NULL DEFAULT 1,
    updated_at             TEXT NOT NULL,
    updated_by             TEXT REFERENCES users(id) ON DELETE SET NULL,
    PRIMARY KEY (org_id, project_id)
);

-- Plain-text emails with {{ placeholders }} from a fixed list (app.mailer.PLACEHOLDERS).
CREATE TABLE IF NOT EXISTS email_templates (
    id         TEXT PRIMARY KEY,
    org_id     TEXT NOT NULL REFERENCES organisations(id),
    name       TEXT NOT NULL,
    subject    TEXT NOT NULL,
    body       TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (org_id, name)
);

CREATE TABLE IF NOT EXISTS onboarding_steps (
    id                TEXT PRIMARY KEY,
    org_id            TEXT NOT NULL REFERENCES organisations(id),
    project_id        TEXT NOT NULL DEFAULT '',     -- '' for the organisation's own workflow
    position          INTEGER NOT NULL,             -- order within the workflow, from 1
    title             TEXT NOT NULL,
    description       TEXT NOT NULL DEFAULT '',
    -- who ticks the step off: the volunteer themselves, or one of the organisation's admins
    -- (its own workflow), or the project's lead side (a project's workflow)
    completed_by      TEXT NOT NULL CHECK (completed_by IN ('volunteer', 'org_admin', 'lead')),
    -- sent to the volunteer when they reach this step
    email_template_id TEXT REFERENCES email_templates(id) ON DELETE SET NULL,
    CHECK (completed_by != CASE project_id WHEN '' THEN 'lead' ELSE 'org_admin' END)
);
CREATE INDEX IF NOT EXISTS onboarding_steps_org
    ON onboarding_steps(org_id, project_id, position);

-- One volunteer going through one workflow. An organisation's is started by joining it and
-- cancelled by leaving or being removed; a project's is started by accepting an invitation to
-- its team and cancelled by leaving or being removed from it. Starting again starts afresh.
CREATE TABLE IF NOT EXISTS onboarding_runs (
    user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    org_id       TEXT NOT NULL REFERENCES organisations(id),
    project_id   TEXT NOT NULL DEFAULT '',
    state        TEXT NOT NULL CHECK (state IN ('in_progress', 'completed', 'cancelled')),
    started_at   TEXT NOT NULL,
    completed_at TEXT,
    PRIMARY KEY (user_id, org_id, project_id)
);
CREATE INDEX IF NOT EXISTS onboarding_runs_org ON onboarding_runs(org_id, project_id, state);

-- A step a volunteer has reached ('open': it is their current step) or finished ('done').
CREATE TABLE IF NOT EXISTS onboarding_progress (
    user_id   TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    step_id   TEXT NOT NULL REFERENCES onboarding_steps(id) ON DELETE CASCADE,
    state     TEXT NOT NULL CHECK (state IN ('open', 'done')),
    opened_at TEXT NOT NULL,
    done_at   TEXT,
    done_by   TEXT REFERENCES users(id) ON DELETE SET NULL,
    PRIMARY KEY (user_id, step_id)
);

-- Emails waiting to be sent, written in the transaction that causes them (app.mailer). The
-- recipient is a user, not an address: the address is read when the message is sent, and never
-- shown to an organisation's admins.
CREATE TABLE IF NOT EXISTS email_outbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id          TEXT NOT NULL REFERENCES organisations(id),
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    purpose         TEXT NOT NULL CHECK (purpose IN ('step', 'completed', 'test')),
    template_name   TEXT NOT NULL DEFAULT '',
    subject         TEXT NOT NULL,
    body            TEXT NOT NULL,
    -- 'not_configured': there was no mail server to send it through (SMTP_HOST unset)
    status          TEXT NOT NULL DEFAULT 'queued' CHECK (status IN (
                        'queued', 'sent', 'failed', 'not_configured')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    created_at      TEXT NOT NULL,
    next_attempt_at TEXT NOT NULL,
    sent_at         TEXT
);
CREATE INDEX IF NOT EXISTS email_outbox_due ON email_outbox(next_attempt_at)
    WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS email_outbox_org ON email_outbox(org_id, id);

-- Retryable projection to Relate. Items name an entity; the projector reconciles that entity
-- from the current SQLite state, so replay is idempotent and order-insensitive.
CREATE TABLE IF NOT EXISTS outbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL CHECK (kind IN (
                        'catalogue', 'organisation', 'volunteer', 'project', 'onboarding')),
    entity_id       TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    next_attempt_at TEXT NOT NULL,
    done_at         TEXT
);
-- Only pending items are ever looked up, so the index covers just those rows. (The index it
-- replaces covered every row the outbox had ever held.)
DROP INDEX IF EXISTS outbox_pending;
CREATE INDEX IF NOT EXISTS outbox_due ON outbox(kind, entity_id) WHERE done_at IS NULL;
"""

# How long finished or expired rows are kept before `prune` deletes them.
OUTBOX_DONE_DAYS = 7
SESSION_ENDED_DAYS = 7
NOTIFICATION_READ_DAYS = 90
EMAIL_KEPT_DAYS = 30  # a message that was sent, or never will be
APPROVAL_WAIT_DAYS = 2  # an assistant run left waiting for approval is abandoned after this


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def connect(path: Path) -> sqlite3.Connection:
    """Open a connection. Kept cheap, because one is opened per unit of work.

    `timeout` is SQLite's busy timeout. WAL mode is a property of the database file, so it is
    set once in `init_schema` and not on every connection.
    """
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)  # explicit transactions
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


SCHEMA_VERSION = 9


class SchemaOutdated(RuntimeError):
    pass


def init_schema(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")  # persistent: stored in the database file
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        has_tables = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'users'"
        ).fetchone()
        if has_tables and version != SCHEMA_VERSION:
            raise SchemaOutdated(
                f"{path} has schema version {version}, expected {SCHEMA_VERSION}. The demo data "
                "is disposable: run `uv run task reset` (or `docker compose down -v`)."
            )
        conn.executescript(SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    finally:
        conn.close()


class Database:
    """Opens a short-lived connection per unit of work (safe across threads and processes)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def init(self) -> None:
        init_schema(self.path)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """One IMMEDIATE transaction: committed on success, rolled back on error."""
        conn = connect(self.path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = connect(self.path)
        try:
            yield conn
        finally:
            conn.close()


def enqueue(conn: sqlite3.Connection, kind: str, entity_id: str) -> None:
    """Add a projection item in the caller's transaction, so it commits with the change."""
    stamp = now()
    conn.execute(
        "INSERT INTO outbox (kind, entity_id, created_at, next_attempt_at) VALUES (?, ?, ?, ?)",
        (kind, entity_id, stamp, stamp),
    )


def days_ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")


def prune(conn: sqlite3.Connection, *, audit_days: int | None = None) -> int:
    """Delete rows nothing reads any more: projected outbox items, sessions that expired or were
    revoked, notifications read long ago, emails that were sent a while back, and audit events
    and assistant runs past their retention (only when `audit_days` is given: start-up passes
    the configured period, the projector's routine prune does not). Unread notifications are
    never deleted. A run nobody approved is closed and its stored conversation dropped."""
    conn.execute(
        "UPDATE agent_runs SET status = 'failed', error = 'approval not given in time', "
        "messages = NULL, pending = '[]', finished_at = ? "
        "WHERE status = 'awaiting_approval' AND created_at < ?",
        (now(), days_ago(APPROVAL_WAIT_DAYS)),
    )
    deleted = conn.execute(
        "DELETE FROM outbox WHERE done_at IS NOT NULL AND done_at < ?",
        (days_ago(OUTBOX_DONE_DAYS),),
    ).rowcount
    ended = days_ago(SESSION_ENDED_DAYS)
    deleted += conn.execute(
        # A user's newest session is kept: sign-in resumes the organisation it had active.
        "DELETE FROM sessions "
        "WHERE (expires_at < ? OR (revoked_at IS NOT NULL AND revoked_at < ?)) "
        "AND EXISTS (SELECT 1 FROM sessions n WHERE n.user_id = sessions.user_id "
        "AND n.created_at > sessions.created_at)",
        (ended, ended),
    ).rowcount
    deleted += conn.execute(
        "DELETE FROM notifications WHERE read_at IS NOT NULL AND read_at < ?",
        (days_ago(NOTIFICATION_READ_DAYS),),
    ).rowcount
    deleted += conn.execute(
        "DELETE FROM email_outbox WHERE status != 'queued' AND created_at < ?",
        (days_ago(EMAIL_KEPT_DAYS),),
    ).rowcount
    if audit_days is not None:
        old = days_ago(audit_days)
        deleted += conn.execute("DELETE FROM audit_log WHERE created_at < ?", (old,)).rowcount
        deleted += conn.execute("DELETE FROM agent_runs WHERE created_at < ?", (old,)).rowcount
    return deleted
