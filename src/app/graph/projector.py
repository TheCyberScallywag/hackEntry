"""Outbox projector: makes Relate catch up with SQLite.

Each outbox item names an entity (a volunteer, a project, an organisation, an organisation's
onboarding workflow or the catalogue). A project's own onboarding workflow is part of the
project: it is reconciled whenever the project is.
The projector reads that entity's *current* state from SQLite and reconciles the graph to it
with idempotent upserts and removal of stale edges, all inside one Relate transaction. Only
then is the item marked done. A failure leaves the item pending with its error and a backoff,
so it is retried by the background loop, the next request, a restart, or `app.cli drain`.

A project becomes `published` (discoverable and matchable) only after its approved revision has
been written to Relate.
"""

import json
import logging
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.graph import cypher
from app.graph.store import GraphError, GraphStore, Tx
from app.notify import notify
from app.services import engagements, teams
from app.sqlstore import Database, now, prune

log = logging.getLogger(__name__)

MAX_BACKOFF_SECONDS = 60
FAILED_AFTER_ATTEMPTS = 3  # surfaced as "failed" in the UI from here on, still retried


@dataclass
class DrainResult:
    done: int = 0
    failed: int = 0


@dataclass
class Changed:
    """The entities one drain projected, for the post-projection hook."""

    volunteers: set[str] = field(default_factory=set)
    projects: set[str] = field(default_factory=set)
    everything: bool = False  # the catalogue or an organisation changed

    def add(self, kind: str, entity_id: str) -> None:
        if kind == "volunteer":
            self.volunteers.add(entity_id)
        elif kind == "project":
            self.projects.add(entity_id)
        elif kind == "onboarding":
            pass  # an organisation's own workflow decides no match
        else:
            self.everything = True


@dataclass
class OutboxStatus:
    pending: int
    failed: int
    items: list[dict]


class Projector:
    def __init__(self, db: Database, graph: GraphStore) -> None:
        self.db = db
        self.graph = graph
        self._drain_lock = threading.Lock()
        # Called after a drain that projected something, with what changed (app.runtime wires
        # the placement sweep). `None` means "anything may have changed".
        self.on_projected: Callable[[Changed | None], object] | None = None
        # True until the hook has run once in this process: a sweep that failed before a restart
        # was only remembered in memory, so the first drain always looks at everything.
        self._hook_due = True
        self._schema_ready = False  # the constraints and indexes are applied once per process

    # ------------------------------------------------------------------ public
    def ensure_schema(self) -> None:
        """Declare the graph's constraints and indexes (app.graph.cypher.SCHEMA). Idempotent."""
        with self.graph.write() as tx:
            for statement in cypher.SCHEMA:
                tx.run(statement)
        self._schema_ready = True

    def drain(self, *, limit: int = 500, ignore_backoff: bool = False) -> DrainResult:
        """Project every due item. Safe to call from any thread; concurrent calls coalesce."""
        result = DrainResult()
        if not self._drain_lock.acquire(blocking=False):
            return result
        try:
            with self.db.read() as conn:
                due = "" if ignore_backoff else "AND next_attempt_at <= ?"
                params: tuple = () if ignore_backoff else (now(),)
                rows = conn.execute(
                    "SELECT id, kind, entity_id FROM outbox WHERE done_at IS NULL "
                    f"{due} ORDER BY id LIMIT {int(limit)}",
                    params,
                ).fetchall()
            # Coalesce: one reconcile per entity covers every earlier item for it.
            groups: dict[tuple[str, str], list[int]] = {}
            for row in rows:
                groups.setdefault((row["kind"], row["entity_id"]), []).append(row["id"])
            # Catalogue and organisations first so volunteer/project edges have targets.
            order = {
                "catalogue": 0,
                "organisation": 1,
                "volunteer": 2,
                "project": 3,
                "onboarding": 4,
            }
            changed = Changed()
            if not self._schema_ready and (groups or self.graph.path.exists()):
                try:
                    self.ensure_schema()
                except GraphError as error:
                    # Nothing is written to a store whose schema could not be declared: every
                    # item stays pending with the reason, and is retried like any other failure.
                    log.warning("declaring the graph schema failed: %s", error)
                    for ids in groups.values():
                        self._mark_failed(ids, str(error))
                        result.failed += len(ids)
                    return result
            for (kind, entity_id), ids in sorted(
                groups.items(), key=lambda g: (order[g[0][0]], min(g[1]))
            ):
                try:
                    self._project(kind, entity_id)
                except (GraphError, sqlite3.Error, ValueError) as error:
                    log.warning("projection of %s %s failed: %s", kind, entity_id, error)
                    self._mark_failed(ids, str(error))
                    result.failed += len(ids)
                else:
                    self._mark_done(ids)
                    result.done += len(ids)
                    changed.add(kind, entity_id)
            if (result.done or self._hook_due) and self.on_projected:
                # A replay, a retry after a failed hook, or a catalogue or organisation change
                # can affect any pair, so those look at everything.
                everything = ignore_backoff or self._hook_due or changed.everything
                # Due again until it has finished: the items are already marked done, so nothing
                # else would bring a failed sweep back, whatever it failed with.
                self._hook_due = True
                try:
                    self.on_projected(None if everything else changed)
                    self._hook_due = False
                except (GraphError, sqlite3.Error) as error:
                    log.warning("post-projection hook failed: %s", error)
            if result.done:
                with self.db.tx() as conn:
                    prune(conn)
        finally:
            self._drain_lock.release()
        return result

    def status(self, limit: int = 50) -> OutboxStatus:
        with self.db.read() as conn:
            pending = conn.execute("SELECT count(*) FROM outbox WHERE done_at IS NULL").fetchone()[
                0
            ]
            failed = conn.execute(
                "SELECT count(*) FROM outbox WHERE done_at IS NULL AND attempts >= ?",
                (FAILED_AFTER_ATTEMPTS,),
            ).fetchone()[0]
            items = [
                dict(r)
                for r in conn.execute(
                    "SELECT id, kind, entity_id, attempts, last_error, created_at, "
                    "next_attempt_at FROM outbox WHERE done_at IS NULL ORDER BY id LIMIT ?",
                    (limit,),
                )
            ]
        return OutboxStatus(pending=pending, failed=failed, items=items)

    # ------------------------------------------------------------------ bookkeeping
    def _mark_done(self, ids: list[int]) -> None:
        marks = ",".join("?" * len(ids))
        with self.db.tx() as conn:
            conn.execute(
                f"UPDATE outbox SET done_at = ?, last_error = NULL WHERE id IN ({marks})",
                (now(), *ids),
            )

    def _mark_failed(self, ids: list[int], error: str) -> None:
        marks = ",".join("?" * len(ids))
        with self.db.tx() as conn:
            attempts = conn.execute(
                f"SELECT max(attempts) FROM outbox WHERE id IN ({marks})", ids
            ).fetchone()[0]
            delay = min(2 ** (attempts or 0), MAX_BACKOFF_SECONDS)
            retry_at = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat(timespec="seconds")
            conn.execute(
                "UPDATE outbox SET attempts = attempts + 1, last_error = ?, next_attempt_at = ? "
                f"WHERE id IN ({marks})",
                (error[:500], retry_at, *ids),
            )

    # ------------------------------------------------------------------ reconcile
    def _project(self, kind: str, entity_id: str) -> None:
        match kind:
            case "catalogue":
                self._project_catalogue()
            case "organisation":
                self._project_organisation(entity_id)
            case "volunteer":
                self._project_volunteer(entity_id)
            case "project":
                self._project_project(entity_id)
            case "onboarding":
                self._project_onboarding(entity_id)
            case _:
                raise ValueError(f"unknown outbox kind {kind!r}")

    def _project_catalogue(self) -> None:
        with self.db.read() as conn:
            themes = [
                dict(r) for r in conn.execute("SELECT id, name FROM cause_themes ORDER BY id")
            ]
            causes = [
                dict(r) for r in conn.execute("SELECT id, name, theme_id FROM causes ORDER BY id")
            ]
            areas = [
                dict(r)
                for r in conn.execute("SELECT id, name, parent_id FROM skill_areas ORDER BY id")
            ]
            skills = [
                dict(r) | {"alt_labels": json.loads(r["alt_labels"])}
                for r in conn.execute(
                    "SELECT id, name, category, area_id, kind, alt_labels FROM skills ORDER BY id"
                )
            ]
            related = conn.execute(
                "SELECT skill_id AS a, related_id AS b FROM skill_related ORDER BY a, b"
            ).fetchall()
        edges = {
            "IN_THEME": [{"a": c["id"], "b": c["theme_id"]} for c in causes],
            "IN_AREA": [{"a": s["id"], "b": s["area_id"]} for s in skills],
            "BROADER": [{"a": a["id"], "b": a["parent_id"]} for a in areas if a["parent_id"]],
            "RELATED_TO": [dict(r) for r in related],
        }
        with self.graph.write() as tx:
            tx.run(cypher.UPSERT_CAUSE_THEMES, {"rows": themes})
            tx.run(cypher.UPSERT_CAUSES, {"rows": causes})
            tx.run(cypher.UPSERT_SKILL_AREAS, {"rows": areas})
            tx.run(cypher.UPSERT_SKILLS, {"rows": skills})
            tx.run(cypher.CATALOGUE_PRUNE["CauseTheme"], {"ids": [t["id"] for t in themes]})
            tx.run(cypher.CATALOGUE_PRUNE["SkillArea"], {"ids": [a["id"] for a in areas]})
            for rel in cypher.CATALOGUE_RELS:
                tx.run(cypher.CATALOGUE_EDGES_CLEAR[rel])
                tx.run(cypher.CATALOGUE_EDGES_MERGE[rel], {"rows": edges[rel]})

    def _project_organisation(self, org_id: str) -> None:
        with self.db.read() as conn:
            org = conn.execute(
                "SELECT id, name FROM organisations WHERE id = ?", (org_id,)
            ).fetchone()
        if org is None:
            raise ValueError(f"organisation {org_id} not found")
        with self.graph.write() as tx:
            tx.run(cypher.UPSERT_ORGANISATION, {"id": org["id"], "name": org["name"]})

    def _project_volunteer(self, user_id: str) -> None:
        with self.db.read() as conn:
            user = conn.execute("SELECT status FROM users WHERE id = ?", (user_id,)).fetchone()
            if user is None:
                raise ValueError(f"user {user_id} not found")
            # Membership edges exist only for vetted (active) volunteers, so screening,
            # declined or suspended accounts can never match.
            orgs = [
                r[0]
                for r in conn.execute(
                    "SELECT org_id FROM memberships WHERE user_id = ? AND opted_in = 1",
                    (user_id,),
                )
            ] if user["status"] == "active" else []  # fmt: skip
            # Consent as an edge: only organisations the volunteer is with, and chose.
            sharing = [
                r[0]
                for r in conn.execute(
                    "SELECT org_id FROM memberships WHERE user_id = ? AND opted_in = 1 "
                    "AND share_history = 1",
                    (user_id,),
                )
            ] if user["status"] == "active" else []  # fmt: skip
            skills = conn.execute(
                "SELECT skill_id, has, offers, excluded, level FROM profile_skills "
                "WHERE user_id = ?",
                (user_id,),
            ).fetchall()
            causes = conn.execute(
                "SELECT cause_id, stance FROM profile_causes WHERE user_id = ?", (user_id,)
            ).fetchall()
            learning = [
                r[0]
                for r in conn.execute(
                    "SELECT skill_id FROM profile_learning WHERE user_id = ?", (user_id,)
                )
            ]
        targets = {
            "VOLUNTEERS_WITH": orgs,
            "HAS_SKILL": [s["skill_id"] for s in skills if s["has"] and not s["excluded"]],
            "OFFERS_SKILL": [s["skill_id"] for s in skills if s["offers"] and not s["excluded"]],
            "EXCLUDES_SKILL": [s["skill_id"] for s in skills if s["excluded"]],
            "PREFERS_CAUSE": [c["cause_id"] for c in causes if c["stance"] == "prefer"],
            "EXCLUDES_CAUSE": [c["cause_id"] for c in causes if c["stance"] == "exclude"],
            "SHARES_HISTORY_WITH": sharing,
            "WANTS_TO_LEARN": learning,
        }
        levels = [
            {"skill": s["skill_id"], "level": s["level"]}
            for s in skills
            if s["has"] and not s["excluded"]
        ]
        with self.graph.write() as tx:
            tx.run(cypher.VOLUNTEER_UPSERT, {"id": user_id})
            for rel in cypher.VOLUNTEER_RELS:
                params = {"id": user_id, "targets": sorted(targets[rel])}
                tx.run(cypher.VOLUNTEER_EDGE_PRUNE[rel], params)
                tx.run(cypher.VOLUNTEER_EDGE_MERGE[rel], params)
            tx.run(cypher.VOLUNTEER_SKILL_LEVELS, {"id": user_id, "rows": levels})

    def _project_project(self, submission_id: str) -> None:
        with self.db.tx() as conn:
            teams.ensure_lead_on_team(conn, submission_id)  # the lead needs no second yes
            engagements.sync(conn, submission_id)  # history follows the team
        with self.db.read() as conn:
            sub = conn.execute(
                "SELECT id, org_id, state, approved_revision FROM submissions WHERE id = ?",
                (submission_id,),
            ).fetchone()
            if sub is None:
                raise ValueError(f"submission {submission_id} not found")
            live = sub["state"] in ("approved", "published") and sub["approved_revision"]
            kept = live or sub["state"] == "completed"  # a completed project stays, as history
            if kept:
                rev = conn.execute(
                    "SELECT title, summary, cause_id FROM submission_revisions "
                    "WHERE submission_id = ? AND revision = ?",
                    (submission_id, sub["approved_revision"]),
                ).fetchone()
                skills = conn.execute(
                    "SELECT skill_id, required FROM revision_skills "
                    "WHERE submission_id = ? AND revision = ? ORDER BY skill_id",
                    (submission_id, sub["approved_revision"]),
                ).fetchall()
                team = [
                    r[0]
                    for r in conn.execute(
                        "SELECT volunteer_id FROM placements WHERE project_id = ? "
                        "AND state = 'team' ORDER BY volunteer_id",
                        (submission_id,),
                    )
                ]
                flow = self._workflow(conn, sub["org_id"], submission_id)
                history = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT id, volunteer_id AS volunteer, role, outcome, readiness "
                        "FROM engagements WHERE project_id = ? ORDER BY id",
                        (submission_id,),
                    )
                ]
                shown = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT k.engagement_id AS engagement, k.skill_id AS skill, k.level "
                        "FROM engagement_skills k JOIN engagements e ON e.id = k.engagement_id "
                        "WHERE e.project_id = ? ORDER BY k.engagement_id, k.skill_id",
                        (submission_id,),
                    )
                ]
        if not kept:
            with self.graph.write() as tx:
                tx.run(cypher.PROJECT_STEPS_REMOVE, {"id": submission_id})
                tx.run(cypher.PROJECT_ENGAGEMENTS_REMOVE, {"id": submission_id})
                tx.run(cypher.PROJECT_REMOVE, {"id": submission_id})
            return
        if not live:  # completed: nobody is on its team or in its onboarding any more
            team, flow = [], {"enabled": False, "steps": [], "ids": [], "edges": {}}
        targets = {
            "SERVES_CAUSE": [rev["cause_id"]],
            "REQUIRES_SKILL": [s["skill_id"] for s in skills if s["required"]],
            "BENEFITS_FROM_SKILL": [s["skill_id"] for s in skills if not s["required"]],
        }
        with self.graph.write() as tx:
            self._upsert_project(tx, sub, rev, targets)
            tx.run(cypher.PROJECT_TEAM_PRUNE, {"id": submission_id, "targets": team})
            tx.run(cypher.PROJECT_TEAM_MERGE, {"id": submission_id, "targets": team})
            # The project's own onboarding: its switch, its steps and who is where in them.
            key = {"id": submission_id}
            tx.run(cypher.PROJECT_ONBOARDING, key | {"enabled": flow["enabled"]})
            tx.run(cypher.PROJECT_STEPS_PRUNE, key | {"ids": flow["ids"]})
            tx.run(cypher.PROJECT_STEPS_UPSERT, key | {"rows": flow["steps"]})
            for statement in cypher.PROJECT_STEP_EDGES_CLEAR.values():
                tx.run(statement, key)
            self._merge_workflow_edges(tx, flow)
            tx.run(cypher.PROJECT_ENGAGEMENTS_PRUNE, key | {"ids": [e["id"] for e in history]})
            tx.run(cypher.PROJECT_ENGAGEMENTS_UPSERT, key | {"rows": history})
            tx.run(cypher.PROJECT_DEMONSTRATED_CLEAR, key)
            tx.run(cypher.PROJECT_DEMONSTRATED_MERGE, {"rows": shown})
        if sub["state"] == "approved":
            stamp = now()
            with self.db.tx() as conn:
                published = conn.execute(
                    "UPDATE submissions SET state = 'published', published_at = ?, "
                    "updated_at = ?, version = version + 1 "
                    "WHERE id = ? AND state = 'approved' AND approved_revision = ?",
                    (stamp, stamp, submission_id, sub["approved_revision"]),
                ).rowcount
                if published:
                    author = conn.execute(
                        "SELECT author_id FROM submissions WHERE id = ?", (submission_id,)
                    ).fetchone()[0]
                    notify(conn, author, "project",
                           f"“{rev['title']}” is now active: visible and matching volunteers.",
                           f"/o/{sub['org_id']}/projects/{submission_id}")  # fmt: skip

    @staticmethod
    def _workflow(conn: sqlite3.Connection, org_id: str, project_id: str = "") -> dict:
        """One workflow as the graph holds it: the organisation's own, or one project's. Its
        switch, its steps in order, and the step each volunteer has reached or completed.
        Email templates stay in SQLite."""
        scope = (org_id, project_id)
        flow = conn.execute(
            "SELECT enabled FROM onboarding_workflows WHERE org_id = ? AND project_id = ?", scope
        ).fetchone()
        steps = [
            dict(r) | {"sends_email": r["sends_email"] == 1}
            for r in conn.execute(
                "SELECT id, title, description, position, completed_by, "
                "email_template_id IS NOT NULL AS sends_email FROM onboarding_steps "
                "WHERE org_id = ? AND project_id = ? ORDER BY position, id",
                scope,
            )
        ]
        progress = conn.execute(
            "SELECT p.user_id, p.step_id, p.state FROM onboarding_progress p "
            "JOIN onboarding_steps s ON s.id = p.step_id "
            "JOIN onboarding_runs r ON r.user_id = p.user_id AND r.org_id = s.org_id "
            "  AND r.project_id = s.project_id "
            "WHERE s.org_id = ? AND s.project_id = ? AND r.state != 'cancelled' "
            "AND (p.state = 'done' OR r.state = 'in_progress') ORDER BY p.user_id, p.step_id",
            scope,
        ).fetchall()
        return {
            "enabled": bool(flow and flow["enabled"]),
            "steps": steps,
            "ids": [s["id"] for s in steps],
            "edges": {
                "AT_STEP": [r for r in progress if r["state"] == "open"],
                "COMPLETED_STEP": [r for r in progress if r["state"] == "done"],
            },
        }

    @staticmethod
    def _merge_workflow_edges(tx: Tx, flow: dict) -> None:
        ids = flow["ids"]
        pairs = [{"a": a, "b": b} for a, b in zip(ids, ids[1:], strict=False)]
        tx.run(cypher.ONBOARDING_NEXT_MERGE, {"rows": pairs})
        for rel, rows in flow["edges"].items():
            tx.run(
                cypher.ONBOARDING_PROGRESS_MERGE[rel],
                {"rows": [{"volunteer": r["user_id"], "step": r["step_id"]} for r in rows]},
            )

    def _project_onboarding(self, org_id: str) -> None:
        """One organisation's own onboarding workflow."""
        with self.db.read() as conn:
            flow = self._workflow(conn, org_id)
        with self.graph.write() as tx:
            found = tx.read(cypher.ONBOARDING_ORG, {"org": org_id, "enabled": flow["enabled"]})
            if not found or found[0]["n"] == 0:
                raise GraphError(f"organisation {org_id} is not in the graph yet")
            tx.run(cypher.ONBOARDING_STEPS_PRUNE, {"org": org_id, "ids": flow["ids"]})
            tx.run(cypher.ONBOARDING_STEPS_UPSERT, {"org": org_id, "rows": flow["steps"]})
            for statement in cypher.ONBOARDING_EDGES_CLEAR.values():
                tx.run(statement, {"org": org_id})
            self._merge_workflow_edges(tx, flow)

    @staticmethod
    def _upsert_project(tx: Tx, sub: sqlite3.Row, rev: sqlite3.Row, targets: dict) -> None:
        tx.run(
            cypher.PROJECT_UPSERT,
            {
                "id": sub["id"],
                "org": sub["org_id"],
                "title": rev["title"],
                "summary": rev["summary"],
                "revision": sub["approved_revision"],
                "status": "completed" if sub["state"] == "completed" else "published",
            },
        )
        for rel, ids in targets.items():
            params = {"id": sub["id"], "targets": sorted(ids)}
            tx.run(cypher.PROJECT_EDGE_PRUNE[rel], params)
            tx.run(cypher.PROJECT_EDGE_MERGE[rel], params)
        owned = tx.read(cypher.PROJECT_OWNED, {"id": sub["id"], "org": sub["org_id"]})
        if not owned or owned[0]["n"] == 0:
            raise GraphError(f"organisation {sub['org_id']} is not in the graph yet")


def pending_for(conn: sqlite3.Connection, kind: str, entity_id: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM outbox WHERE done_at IS NULL AND kind = ? AND entity_id = ? LIMIT 1",
            (kind, entity_id),
        ).fetchone()
        is not None
    )
