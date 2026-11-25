"""Wiring (SQLite + Relate + projector) and the disposable demo seed."""

import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app import observability, placements, services
from app.auth import hash_password
from app.catalogue import get_catalogue, load_demo, stable_id
from app.config import BUNDLED_GRAPH, Settings
from app.graph.projector import Changed, Projector
from app.graph.store import GraphStore
from app.sqlstore import Database, enqueue, now, prune

log = logging.getLogger(__name__)

# Every demo account shares this password. It only ever exists in the disposable local demo:
# `seed_demo` refuses to run when APP_ENV=production.
DEMO_PASSWORD = "demo-password-1"  # noqa: S105


@dataclass
class Runtime:
    settings: Settings
    db: Database
    graph: GraphStore
    projector: Projector
    # The Pydantic AI model the assistants use (app.agents); None switches them off. Tests put a
    # TestModel or FunctionModel here.
    agent_model: Any = None


def build_runtime(settings: Settings) -> Runtime:
    db = Database(settings.sqlite_path)
    graph = GraphStore(settings.graph_path, max_rows=settings.graph_max_rows)
    projector = Projector(db, graph)

    def reconcile_placements(changed: Changed | None) -> None:
        scope = None
        if changed is not None:
            scope = placements.Scope(frozenset(changed.volunteers), frozenset(changed.projects))
        with observability.span("placements sweep", scoped=scope is not None) as span:
            span.set_attribute("changes", placements.sweep(db, graph, scope))

    projector.on_projected = reconcile_placements
    from app.agents.deps import build_model  # imported here: app.agents imports this module

    return Runtime(settings, db, graph, projector, build_model(settings))


def chase_invitations(rt: Runtime) -> int:
    """Remind volunteers of unanswered invitations and close the ones that ran out of time."""
    return placements.chase(
        rt.db,
        remind_days=rt.settings.invitation_reminder_days,
        expire_days=rt.settings.invitation_expiry_days,
    )


DEMO_DOMAIN = "cyberscallywags.uk"  # every account here is a demo or test account


def _holds_demo_accounts(conn) -> bool:
    """Whether any account is one the demo seeds. They share a published password, so a
    production database must hold none. The fixture's own addresses are checked one by one,
    because not all of them are at the demo domain."""
    emails = sorted({p["email"].strip().lower() for p in load_demo()["people"]})
    marks = ", ".join("?" * len(emails))
    return bool(
        conn.execute(
            f"SELECT 1 FROM users WHERE email LIKE ? OR email IN ({marks}) LIMIT 1",  # noqa: S608
            (f"%@{DEMO_DOMAIN}", *emails),
        ).fetchone()
    )


def bootstrap(rt: Runtime) -> None:
    """Create the schema; seed the demo on an empty data dir (test env only); replay the outbox.

    Replaying here is what makes a restart catch up with anything a crash left pending.
    """
    fresh = not rt.settings.sqlite_path.exists()
    rt.db.init()
    if not rt.settings.demo_allowed:
        with rt.db.read() as conn:
            if _holds_demo_accounts(conn):
                raise RuntimeError(
                    "APP_ENV=production but the database holds demo accounts (an address "
                    f"from data/fixtures/demo.json, or one at @{DEMO_DOMAIN}). Start from an "
                    "empty DATA_DIR."
                )
    if fresh and rt.settings.seed_demo_on_start and rt.settings.demo_allowed:
        seed_demo(rt, copy_bundle=True)
    else:
        seed_catalogue(rt)
    with rt.db.tx() as conn:
        prune(conn, audit_days=rt.settings.audit_retention_days)
    result = rt.projector.drain(ignore_backoff=True)
    log.info("startup replay: %s done, %s failed", result.done, result.failed)
    chase_invitations(rt)


def seed_catalogue(rt: Runtime) -> None:
    """Upsert the shared catalogue; enqueue a projection only if something changed. Nothing is
    deleted except a related pair: a published cause, skill or area is never withdrawn."""
    cat = get_catalogue()

    def upsert(conn, table: str, columns: tuple[str, ...], rows: list[tuple]) -> None:
        others = columns[1:]
        conn.executemany(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))}) "  # noqa: S608
            f"ON CONFLICT (id) DO UPDATE SET {', '.join(f'{c} = excluded.{c}' for c in others)} "
            f"WHERE {' OR '.join(f'{c} IS NOT excluded.{c}' for c in others)}",
            rows,
        )

    with rt.db.tx() as conn:
        before = conn.total_changes
        upsert(conn, "cause_themes", ("id", "name"), [(t.id, t.name) for t in cat.themes])
        upsert(conn, "causes", ("id", "name", "theme_id"),
               [(c.id, c.name, c.theme) for c in cat.causes])  # fmt: skip
        # Parents first: an area refers to its parent.
        areas = sorted(cat.areas, key=lambda a: a.parent is not None)
        upsert(conn, "skill_areas", ("id", "name", "parent_id"),
               [(a.id, a.name, a.parent) for a in areas])  # fmt: skip
        upsert(conn, "skills", ("id", "name", "category", "area_id", "kind", "alt_labels"),
               [(s.id, s.name, s.category, s.area, s.kind, json.dumps(list(s.alt_labels)))
                for s in cat.skills])  # fmt: skip
        stored = {tuple(r) for r in conn.execute("SELECT skill_id, related_id FROM skill_related")}
        if stored != set(cat.related):
            conn.execute("DELETE FROM skill_related")
            conn.executemany("INSERT INTO skill_related VALUES (?, ?)", cat.related)
        if conn.total_changes != before:
            enqueue(conn, "catalogue", "catalogue")


def seed_demo(rt: Runtime, *, copy_bundle: bool) -> None:
    """Load data/fixtures/demo.json into an empty database and project it.

    With copy_bundle, the committed data/demo.rl is first copied to the writable store, so the
    app runs on a copy of the bundled graph; the projection that follows is then a no-op
    reconcile (and repairs any drift).
    """
    if not rt.settings.demo_allowed:
        raise RuntimeError("Demo seeding is disabled when APP_ENV=production.")
    rt.db.init()
    if copy_bundle and BUNDLED_GRAPH.is_file() and not rt.graph.path.exists():
        rt.graph.path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(BUNDLED_GRAPH, rt.graph.path)
        rt.graph.path.chmod(0o600)
    seed_catalogue(rt)
    demo = load_demo()
    names = rt.settings.org_names
    org_ids = {o["key"]: stable_id("org", o["key"]) for o in demo["organisations"]}
    user_ids = {p["key"]: stable_id("u", p["key"]) for p in demo["people"]}
    password_hash = hash_password(DEMO_PASSWORD)
    stamp = now()
    with rt.db.tx() as conn:
        for org in demo["organisations"]:
            conn.execute(
                "INSERT INTO organisations (id, key, name, description) VALUES (?, ?, ?, ?)",
                (org_ids[org["key"]], org["key"], names.get(org["key"], org["name"]),
                 org["description"]),
            )  # fmt: skip
            enqueue(conn, "organisation", org_ids[org["key"]])
            services.onboarding.install_starter(conn, org_ids[org["key"]])
        for person in demo["people"]:
            uid = user_ids[person["key"]]
            conn.execute(
                "INSERT INTO users (id, email, display_name, password_hash, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (uid, person["email"], person["display_name"], password_hash,
                 person.get("status", "active"), stamp),
            )  # fmt: skip
            for role in person.get("system_roles", []):
                conn.execute(
                    "INSERT INTO system_roles (user_id, role, granted_at) VALUES (?, ?, ?)",
                    (uid, role, stamp),
                )
            for key in person["volunteers_with"]:
                conn.execute(
                    "INSERT INTO memberships (user_id, org_id, opted_in, updated_at) "
                    "VALUES (?, ?, 1, ?)",
                    (uid, org_ids[key], stamp),
                )
            for key, roles in person.get("org_roles", {}).items():
                for role in roles:
                    conn.execute(
                        "INSERT INTO org_roles (user_id, org_id, role, granted_at) "
                        "VALUES (?, ?, ?, ?)",
                        (uid, org_ids[key], role, stamp),
                    )
            for skill, state in person["skills"].items():
                conn.execute(
                    "INSERT INTO profile_skills (user_id, skill_id, has, offers, excluded, level) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (uid, skill, *services.skill_flags(state),
                     person.get("levels", {}).get(skill)),
                )  # fmt: skip
            conn.executemany(
                "INSERT INTO profile_learning (user_id, skill_id) VALUES (?, ?)",
                [(uid, skill) for skill in person.get("learning", [])],
            )
            for key in person.get("shares_history_with", []):
                conn.execute(
                    "UPDATE memberships SET share_history = 1 WHERE user_id = ? AND org_id = ?",
                    (uid, org_ids[key]),
                )
            for cause, stance in person["causes"].items():
                conn.execute(
                    "INSERT INTO profile_causes (user_id, cause_id, stance) VALUES (?, ?, ?)",
                    (uid, cause, stance),
                )
            enqueue(conn, "volunteer", uid)
        for sub in demo["submissions"]:
            sid = stable_id("p", sub["key"])
            approved = sub["outcome"] == "approved"
            reviewer = user_ids.get(sub.get("reviewer", ""))
            conn.execute(
                "INSERT INTO submissions (id, org_id, author_id, lead_id, state, current_revision, "
                "approved_revision, approved_by, approved_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)",
                (sid, org_ids[sub["org"]], user_ids[sub["author"]], user_ids[sub["author"]],
                 sub["outcome"],
                 1 if approved else None, reviewer if approved else None,
                 stamp if approved else None, stamp, stamp),
            )  # fmt: skip
            conn.execute(
                "INSERT INTO submission_revisions (submission_id, revision, title, summary, "
                "cause_id, created_at) VALUES (?, 1, ?, ?, ?, ?)",
                (sid, sub["title"], sub["summary"], sub["cause"], stamp),
            )
            conn.executemany(
                "INSERT INTO revision_skills (submission_id, revision, skill_id, required) "
                "VALUES (?, 1, ?, ?)",
                [(sid, s, 1) for s in sub["required"]] + [(sid, s, 0) for s in sub["optional"]],
            )
            if reviewer:
                conn.execute(
                    "INSERT INTO reviews (submission_id, revision, reviewer_id, action, "
                    "reason_for_author, created_at) VALUES (?, 1, ?, ?, ?, ?)",
                    (sid, reviewer, sub["outcome"], sub["reason"], stamp),
                )
            if approved:
                enqueue(conn, "project", sid)
        _seed_history(conn, demo, org_ids, user_ids)
    rt.projector.drain(ignore_backoff=True)  # also runs the placement sweep
    # Teams are formed through the real flow: the lead invites, the volunteer says yes.
    for team in demo.get("teams", []):
        pid, vid = stable_id("p", team["project"]), user_ids[team["volunteer"]]
        with rt.db.read() as conn:
            lead = conn.execute("SELECT lead_id FROM submissions WHERE id = ?", (pid,)).fetchone()
            version = conn.execute(
                "SELECT version FROM placements WHERE project_id = ? AND volunteer_id = ?",
                (pid, vid),
            ).fetchone()[0]
        placements.lead_respond(rt.db, lead[0], pid, vid, "yes", version=version)
        placements.volunteer_respond(rt.db, vid, pid, "yes", version=version + 1)
    # One organisation runs its own onboarding workflow, with a volunteer part-way through it,
    # so the workflow and a volunteer's place in it are in the graph from the start.
    admin = next(
        (user_ids[p["key"]] for p in demo["people"] if "admin" in p.get("system_roles", [])), ""
    )
    for entry in demo.get("onboarding", []):
        org_id, uid = org_ids[entry["org"]], user_ids[entry["volunteer"]]
        with rt.db.tx() as conn:
            conn.execute("UPDATE onboarding_workflows SET enabled = 1 WHERE org_id = ?", (org_id,))
            services.onboarding.start(conn, uid, org_id)
        for _ in range(entry.get("completed", 0)):
            [run] = [r for r in services.onboarding.my_onboarding(rt.db, uid)
                     if r["org_id"] == org_id]  # fmt: skip
            step = next(s for s in run["steps"] if s["state"] == "open")
            services.onboarding.complete_step(rt.db, admin, org_id, step["id"], user_id=uid)
        with rt.db.tx() as conn:  # the seed announces nothing by email
            conn.execute("DELETE FROM email_outbox")
    # One project has its own team onboarding ready, switched off like every organisation's
    # starter workflow. Once its lead switches it on, a volunteer who accepts an invitation goes
    # through these steps, and the lead side then accepts them onto the team.
    for entry in demo.get("project_onboarding", []):
        pid = stable_id("p", entry["project"])
        with rt.db.tx() as conn:
            org_id = conn.execute("SELECT org_id FROM submissions WHERE id = ?", (pid,)).fetchone()
            services.onboarding.install_project_flow(
                conn, org_id[0], pid, entry["steps"], enabled=entry.get("enabled", False)
            )
    rt.projector.drain(ignore_backoff=True)


def _seed_history(conn, demo: dict, org_ids: dict, user_ids: dict) -> None:
    """Finished work, written as the past it is: completed projects, who was on each team and
    how the lead side rated them. Nothing is announced and nobody is matched: the rows are
    exactly what completing a project and rating its team leave behind
    (app.services.engagements), with the fixture's own dates."""
    for item in demo.get("history", []):
        sid, lead = stable_id("p", item["key"]), user_ids[item["lead"]]
        started, done = item["started"], item["completed"]
        conn.execute(
            "INSERT INTO submissions (id, org_id, author_id, lead_id, state, current_revision, "
            "approved_revision, approved_by, approved_at, published_at, completed_by, "
            "completed_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'completed', 1, 1, ?, ?, ?, ?, ?, ?, ?)",
            (sid, org_ids[item["org"]], lead, lead, user_ids[item["reviewer"]], started, started,
             lead, done, started, done),
        )  # fmt: skip
        conn.execute(
            "INSERT INTO submission_revisions (submission_id, revision, title, summary, "
            "cause_id, created_at) VALUES (?, 1, ?, ?, ?, ?)",
            (sid, item["title"], item["summary"], item["cause"], started),
        )
        conn.executemany(
            "INSERT INTO revision_skills (submission_id, revision, skill_id, required) "
            "VALUES (?, 1, ?, ?)",
            [(sid, s, 1) for s in item["required"]] + [(sid, s, 0) for s in item["optional"]],
        )
        for member in item["team"]:
            vid = user_ids[member["volunteer"]]
            conn.execute(
                "INSERT INTO placements (project_id, volunteer_id, state, volunteer_said, "
                "volunteer_said_at, lead_said, lead_said_at, lead_actor_id, invited_at, "
                "created_at, updated_at) "
                "VALUES (?, ?, 'completed', 'yes', ?, 'yes', ?, ?, ?, ?, ?)",
                (sid, vid, started, started, lead, started, started, done),
            )
            eid = services.engagements.engagement_id(sid, vid, 0)
            rated = "readiness" in member
            conn.execute(
                "INSERT INTO engagements (id, project_id, volunteer_id, role, started_at, "
                "ended_at, outcome, readiness, rated_by, rated_at, note_for_volunteer) "
                "VALUES (?, ?, ?, ?, ?, ?, 'completed', ?, ?, ?, ?)",
                (eid, sid, vid, member["role"], started, done, member.get("readiness"),
                 user_ids[member["rated_by"]] if rated else None, done if rated else None,
                 member.get("note", "")),
            )  # fmt: skip
            conn.executemany(
                "INSERT INTO engagement_skills (engagement_id, skill_id, level) VALUES (?, ?, ?)",
                [(eid, skill, level) for skill, level in member.get("skills", {}).items()],
            )
        enqueue(conn, "project", sid)


def reset_demo(settings: Settings) -> Runtime:
    """Delete the runtime data dir's stores and reseed. For the disposable demo only."""
    if not settings.demo_allowed:
        raise RuntimeError("Reset is disabled when APP_ENV=production.")
    for path in (settings.sqlite_path, settings.graph_path):
        for p in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
            if p.is_dir():
                shutil.rmtree(p)
            elif p.exists():
                p.unlink()
    rt = build_runtime(settings)
    seed_demo(rt, copy_bundle=True)
    return rt
