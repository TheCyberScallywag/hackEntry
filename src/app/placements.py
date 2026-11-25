"""Volunteer onboarding, step 2: from candidate to on the team.

1. After the graph changes, `sweep` asks Relate (MATCH_CANDIDATES, the same matching query as
   everywhere else) which active volunteers are eligible for which active projects. Each new pair
   becomes an `eligible` placement: a candidate. Candidates are shown, ranked, to the project
   lead; the volunteer is not told yet.
2. The lead side chooses who to invite. The lead side is the project lead while they are in
   standing, or a team coordinator or org admin of the organisation, whose answer counts the
   same as the lead's.
   Nobody decides on their own place, except the lead, who is on their own team automatically
   (app.services.teams.ensure_lead_on_team).
3. The invited volunteer answers. Yes puts them on the team (`team`), projected to Relate as
   (:Volunteer)-[:ON_TEAM]->(:Project). A no from either side ends it (`declined`).
   If the project has its own onboarding workflow (app.services.onboarding), yes starts that
   first (`onboarding`): the volunteer works through the project's steps, and the lead side
   then accepts them onto the team (`accept`). The organisation's rules come first: a volunteer
   still part-way through the organisation's own onboarding cannot say yes yet, and nobody is
   accepted, or reinstated, who is not an active volunteer of the organisation.
4. A pair that stops matching before the volunteer joined is `lapsed`; it becomes a candidate
   again if it matches again. Team members are never removed by a sweep. They are removed when
   they lose their standing (suspended, or no longer with the organisation): app.services.teams.
5. An invitation left unanswered is chased: one reminder, then it closes and the volunteer goes
   back to the candidate list (`chase`).
6. The lead side can reopen a declined, left or removed placement (`reopen`).
7. The lead side can suspend a team member (`suspended`: off the team, and out of the graph's
   ON_TEAM, until reinstated) or remove them, whether they are on the team, still onboarding
   or suspended.

The lead side is always the same: the project's lead, whom the team coordinators assign
(app.services.submissions.set_project_lead), and the organisation's team coordinators and
org admins themselves.

Every change is written to `placement_events` with who made it, and the people it affects are
notified in-app.
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
from app.services import teams
from app.sqlstore import Database, days_ago, enqueue, now

log = logging.getLogger(__name__)

STATE_LABELS = {
    "eligible": "Candidate",
    "onboarding": "Onboarding for the team",
    "team": "On the team",
    "suspended": "Suspended from the team",
    "declined": "Declined",
    "lapsed": "No longer eligible",
    "removed": "Removed",
    "left": "Left the team",
    "completed": "Project completed",
}
INVITED_LABEL = "Invited: awaiting the volunteer's answer"
REOPENABLE = ("declined", "left", "removed")


_project_info = teams.project_info
_check_lead = teams.check_lead_side  # the lead in standing, a coordinator or an org admin


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
) -> dict[tuple[str, str], dict]:
    """Eligible (project, volunteer) pairs from Relate, each with what the lead ranks by.

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
    current: dict[tuple[str, str], dict] = {}
    if not reads:
        return current
    with graph.reader() as tx:
        for params in reads:
            for row in tx.read(cypher.MATCH_CANDIDATES, params):
                current[(row["project"], row["volunteer"])] = {
                    "matched": json.dumps(sorted(row["matched"])),
                    "optional": json.dumps(sorted(row["optional_matched"])),
                    "preferred": int(bool(row["preferred"])),
                }
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
    """Create, lapse or revive candidates from the current Relate matches. Returns changes.

    With a `scope`, only pairs that involve one of its volunteers or projects are reconciled:
    the cost follows the size of the change, not the size of the system. Without one, every
    published project is reconciled (start-up, `cli drain`, an admin replay).

    The lead side gets one notification per project for its new candidates. Volunteers hear
    nothing until they are invited, and are told of a lapse only if they had been.
    """
    if scope is not None and not scope:
        return 0
    current = _current_matches(db, graph, scope)  # before the transaction: no lock held meanwhile
    changes = 0
    stamp = now()
    with db.tx() as conn:
        existing = _existing(conn, scope)
        # SQL freshness for the pairs in hand. A scoped sweep asks only about those projects and
        # volunteers, so its cost follows the change; a full sweep reads both tables once.
        projects = sorted({pid for pid, _ in current})
        volunteers = sorted({vid for _, vid in current})
        only_p = f" AND id IN ({_marks(projects)})" if scope is not None else ""
        only_v = f" AND id IN ({_marks(volunteers)})" if scope is not None else ""
        live = {
            r["id"]: r["lead_id"]
            for r in conn.execute(
                f"SELECT id, lead_id FROM submissions WHERE state = 'published'{only_p}",
                projects if scope is not None else (),
            )
        }
        active = {
            r["id"]
            for r in conn.execute(
                f"SELECT id FROM users WHERE status = 'active'{only_v}",
                volunteers if scope is not None else (),
            )
        }
        fresh: dict[str, list[str]] = {}  # project -> new candidates, for one notification each
        for (pid, vid), match in sorted(current.items()):
            if pid not in live or vid not in active:
                continue  # SQL freshness: only active projects and vetted volunteers
            before = existing.get((pid, vid))
            if live[pid] == vid and (not before or before["state"] in ("eligible", "lapsed")):
                # The lead is never a candidate for their own project. Normally the projection
                # has already put them on the team; this catches a lead who only now matches.
                if teams.ensure_lead_on_team(conn, pid):
                    enqueue(conn, "project", pid)  # adds the ON_TEAM edge
                    changes += 1
                continue
            ranking = (match["matched"], match["optional"], match["preferred"])
            if before and before["state"] == "eligible":
                # Still a candidate: keep what the lead ranks by up to date. No version bump,
                # so an open form on either side stays valid.
                if ranking != (
                    before["matched_skills"], before["optional_skills"], before["preferred"]
                ):  # fmt: skip
                    conn.execute(
                        "UPDATE placements SET matched_skills = ?, optional_skills = ?, "
                        "preferred = ?, updated_at = ? WHERE project_id = ? AND volunteer_id = ?",
                        (*ranking, stamp, pid, vid),
                    )
                continue
            if before and before["state"] != "lapsed":
                continue
            if before:
                conn.execute(
                    "UPDATE placements SET state = 'eligible', volunteer_said = NULL, "
                    "volunteer_said_at = NULL, lead_said = NULL, lead_said_at = NULL, "
                    "lead_actor_id = NULL, lead_note = '', reminded_at = NULL, "
                    "matched_skills = ?, optional_skills = ?, preferred = ?, "
                    "version = version + 1, updated_at = ? "
                    "WHERE project_id = ? AND volunteer_id = ?",
                    (*ranking, stamp, pid, vid),
                )
            else:
                conn.execute(
                    "INSERT INTO placements (project_id, volunteer_id, state, matched_skills, "
                    "optional_skills, preferred, created_at, updated_at) "
                    "VALUES (?, ?, 'eligible', ?, ?, ?, ?, ?)",
                    (pid, vid, *ranking, stamp, stamp),
                )
            teams.record(conn, pid, vid, "revived" if before else "matched")
            fresh.setdefault(pid, []).append(vid)
            changes += 1
        for pid, vids in fresh.items():
            info = _project_info(conn, pid)
            if len(vids) == 1:
                name = conn.execute(
                    "SELECT display_name FROM users WHERE id = ?", (vids[0],)
                ).fetchone()[0]
                message = (f"{name} is a new candidate for “{info['title']}”. "
                           "Invite them if you want them on the team.")  # fmt: skip
            else:
                message = (f"{len(vids)} new candidates for “{info['title']}”. "
                           "Invite who you want from the ranked list.")  # fmt: skip
            teams.notify_deciders(conn, info["org_id"], info["lead_id"], message,
                                  teams.project_link(info["org_id"], pid))  # fmt: skip
        for (pid, vid), row in existing.items():
            if row["state"] == "eligible" and (pid, vid) not in current:
                conn.execute(
                    "UPDATE placements SET state = 'lapsed', version = version + 1, "
                    "updated_at = ? WHERE project_id = ? AND volunteer_id = ?",
                    (stamp, pid, vid),
                )
                teams.record(conn, pid, vid, "lapsed")
                if row["lead_said"] == "yes":  # they had been invited, so they are told
                    info = _project_info(conn, pid)
                    notify(conn, vid, "placement",
                           f"You are no longer eligible for “{info['title']}”, so that "
                           "invitation has closed. Your profile or the project changed.",
                           "/opportunities")  # fmt: skip
                changes += 1
    return changes


def _respond(
    db: Database,
    project_id: str,
    volunteer_id: str,
    side: str,
    answer: str,
    version: int,
    *,
    actor_id: str,
    note: str = "",
) -> str:
    if answer not in ("yes", "no"):
        raise services.Invalid("Answer yes or no.")
    note = note.strip()
    if len(note) > services.REASON_MAX:
        raise services.Invalid("That reason is too long.")
    stamp = now()
    with db.tx() as conn:
        if side == "lead":  # authorised first, in the same transaction as the write
            _check_lead(conn, actor_id, project_id, volunteer_id)
        row = conn.execute(
            "SELECT * FROM placements WHERE project_id = ? AND volunteer_id = ?",
            (project_id, volunteer_id),
        ).fetchone()
        if row is None:
            raise services.NotFound("placement")
        if row["state"] != "eligible" or row["version"] != version:
            raise services.Conflict("This offer has changed since you opened it.")
        info = _project_info(conn, project_id)
        if side == "volunteer" and row["lead_said"] != "yes":
            raise services.Conflict("The project lead has not invited you to this team yet.")
        if answer == "yes":
            # The placement's state is not proof on its own: the graph may be behind SQLite.
            if info["state"] != "published":
                raise services.Conflict("This project is not active any more.")
            if not teams.in_standing(conn, volunteer_id, info["org_id"]):
                raise services.Conflict(
                    "This volunteer is not an active volunteer of the organisation any more."
                )
            if side == "volunteer" and services.onboarding.pending_with_org(
                conn, volunteer_id, info["org_id"]
            ):
                raise services.Conflict(
                    f"Finish your onboarding with {info['org_name']} first. The invitation "
                    "stays open meanwhile."
                )
        said = {"volunteer": row["volunteer_said"], "lead": row["lead_said"]} | {side: answer}
        if "no" in said.values():
            state = "declined"
        elif said["volunteer"] == "yes" and said["lead"] == "yes":
            # The project's own onboarding comes before the team, if it has one switched on.
            started = services.onboarding.start(conn, volunteer_id, info["org_id"], project_id)
            state = "onboarding" if started else "team"
        else:
            state = "eligible"
        if side == "lead":
            conn.execute(
                "UPDATE placements SET lead_said = ?, lead_said_at = ?, lead_actor_id = ?, "
                "lead_note = ?, invited_at = CASE WHEN ? = 'yes' THEN COALESCE(invited_at, ?) "
                "ELSE invited_at END, reminded_at = NULL, state = ?, version = version + 1, "
                "updated_at = ? WHERE project_id = ? AND volunteer_id = ?",
                (answer, stamp, actor_id, note, answer, stamp, state, stamp,
                 project_id, volunteer_id),
            )  # fmt: skip
        else:
            conn.execute(
                "UPDATE placements SET volunteer_said = ?, volunteer_said_at = ?, state = ?, "
                "version = version + 1, updated_at = ? WHERE project_id = ? AND volunteer_id = ?",
                (answer, stamp, state, stamp, project_id, volunteer_id),
            )
        was_invited = row["lead_said"] == "yes"
        event = {
            ("lead", "yes"): "invited",
            ("lead", "no"): "invitation_withdrawn" if was_invited else "passed_over",
            ("volunteer", "yes"): "onboarding_started" if state == "onboarding" else "joined",
            ("volunteer", "no"): "declined",
        }[(side, answer)]
        teams.record(conn, project_id, volunteer_id, event, actor_id=actor_id, note=note)
        name = conn.execute(
            "SELECT display_name FROM users WHERE id = ?", (volunteer_id,)
        ).fetchone()[0]
        title = info["title"]
        link = teams.project_link(info["org_id"], project_id)
        inviter = actor_id if side == "lead" else row["lead_actor_id"]

        def tell_deciders(message: str) -> None:
            teams.notify_deciders(conn, info["org_id"], info["lead_id"], message, link,
                                  exclude=(volunteer_id, actor_id), also=(inviter,))  # fmt: skip

        if state == "team":
            notify(conn, volunteer_id, "placement",
                   f"You are on the team for “{title}”.", "/opportunities")  # fmt: skip
            tell_deciders(f"{name} joined the team for “{title}”.")
            enqueue(conn, "project", project_id)  # adds the ON_TEAM edge in Relate
        elif state == "onboarding":  # the volunteer was told of their first step by the run
            tell_deciders(
                f"{name} said yes to “{title}” and has started its onboarding steps. You "
                "accept them onto the team once they have finished."
            )
        elif state == "declined":
            if side == "volunteer":
                tell_deciders(f"{name} declined the invitation to “{title}”.")
            elif was_invited or row["volunteer_said"] == "yes":
                # A candidate who was passed over without being invited was never told of the
                # match, so there is nothing to tell them now.
                notify(conn, volunteer_id, "placement",
                       f"The invitation to join “{title}” has been withdrawn"
                       + (f": {note}" if note else "."), "/opportunities")  # fmt: skip
        else:  # the lead side invited
            notify(conn, volunteer_id, "placement",
                   f"You are invited to join the team for “{title}” ({info['org_name']}). "
                   "Say yes to join.", "/opportunities")  # fmt: skip
    return state


def volunteer_respond(
    db: Database, user_id: str, project_id: str, answer: str, *, version: int
) -> str:
    """The invited volunteer answers for themselves (their own placement only)."""
    services.require_active(db, user_id)
    return _respond(db, project_id, user_id, "volunteer", answer, version, actor_id=user_id)


def lead_respond(
    db: Database,
    actor_id: str,
    project_id: str,
    volunteer_id: str,
    answer: str,
    *,
    version: int,
    note: str = "",
) -> str:
    """The lead side answers for the project: yes invites the candidate, no passes them over
    or withdraws an invitation already sent. `note` is an optional reason, kept on record and
    shown to the volunteer when an invitation is withdrawn."""
    return _respond(
        db, project_id, volunteer_id, "lead", answer, version, actor_id=actor_id, note=note
    )


def reopen(db: Database, actor_id: str, project_id: str, volunteer_id: str) -> None:
    """The lead side gives a declined, left or removed placement another chance.

    It becomes `lapsed`, and the sweep that follows makes the volunteer a candidate again if
    they still match (or puts them straight back on the team if they lead the project)."""
    with db.tx() as conn:
        info = _check_lead(conn, actor_id, project_id, volunteer_id)
        row = conn.execute(
            "SELECT state FROM placements WHERE project_id = ? AND volunteer_id = ?",
            (project_id, volunteer_id),
        ).fetchone()
        if row is None:
            raise services.NotFound("placement")
        if row["state"] not in REOPENABLE:
            raise services.Conflict("This placement is not closed, so there is nothing to reopen.")
        if info["state"] != "published":
            raise services.Conflict("This project is not active any more.")
        if not teams.in_standing(conn, volunteer_id, info["org_id"]):
            raise services.Invalid(
                "They are not an active volunteer of this organisation at the moment."
            )
        conn.execute(
            "UPDATE placements SET state = 'lapsed', volunteer_said = NULL, "
            "volunteer_said_at = NULL, lead_said = NULL, lead_said_at = NULL, "
            "lead_actor_id = NULL, lead_note = '', reminded_at = NULL, version = version + 1, "
            "updated_at = ? WHERE project_id = ? AND volunteer_id = ?",
            (now(), project_id, volunteer_id),
        )
        teams.record(conn, project_id, volunteer_id, "reopened", actor_id=actor_id)
        enqueue(conn, "project", project_id)  # the projection's sweep revives it if it matches


def chase(db: Database, *, remind_days: int, expire_days: int) -> int:
    """Chase invitations nobody answered. Returns how many were reminded or closed.

    After `remind_days` the volunteer is reminded once. After `expire_days` the invitation
    closes: the volunteer goes back to the candidate list and both sides are told. A value of 0
    switches that step off."""
    changes = 0
    stamp = now()
    open_invitation = (
        "SELECT p.project_id, p.volunteer_id, p.lead_actor_id, s.title, s.org_id, s.lead_id, "
        "u.display_name FROM placements p JOIN submission_current s ON s.id = p.project_id "
        "JOIN users u ON u.id = p.volunteer_id "
        "WHERE p.state = 'eligible' AND p.lead_said = 'yes' AND p.lead_said_at < ?"
    )
    with db.tx() as conn:
        if expire_days > 0:
            for row in conn.execute(open_invitation, (days_ago(expire_days),)).fetchall():
                pid, vid = row["project_id"], row["volunteer_id"]
                conn.execute(
                    "UPDATE placements SET lead_said = NULL, lead_said_at = NULL, "
                    "lead_actor_id = NULL, lead_note = '', reminded_at = NULL, "
                    "version = version + 1, updated_at = ? "
                    "WHERE project_id = ? AND volunteer_id = ?",
                    (stamp, pid, vid),
                )
                teams.record(conn, pid, vid, "invitation_expired")
                notify(conn, vid, "placement",
                       f"Your invitation to join “{row['title']}” was not answered in time and "
                       "has closed.", "/opportunities")  # fmt: skip
                teams.notify_deciders(
                    conn, row["org_id"], row["lead_id"],
                    f"{row['display_name']} did not answer the invitation to “{row['title']}”. "
                    "They are back in the candidate list.",
                    teams.project_link(row["org_id"], pid), also=(row["lead_actor_id"],),
                )  # fmt: skip
                changes += 1
        if remind_days > 0:
            for row in conn.execute(
                open_invitation + " AND p.reminded_at IS NULL", (days_ago(remind_days),)
            ).fetchall():
                conn.execute(
                    "UPDATE placements SET reminded_at = ? WHERE project_id = ? "
                    "AND volunteer_id = ?",
                    (stamp, row["project_id"], row["volunteer_id"]),
                )  # no version bump: the volunteer's open form stays valid
                closes = (
                    f" It closes {expire_days - remind_days} days from now."
                    if expire_days > remind_days
                    else ""
                )
                notify(conn, row["volunteer_id"], "placement",
                       f"Reminder: you are invited to join “{row['title']}” and have not "
                       f"answered yet.{closes}", "/opportunities")  # fmt: skip
                changes += 1
    return changes


def _name(conn: sqlite3.Connection, user_id: str) -> str:
    return conn.execute("SELECT display_name FROM users WHERE id = ?", (user_id,)).fetchone()[0]


def _may_hold_place(conn: sqlite3.Connection, info: sqlite3.Row, volunteer_id: str) -> None:
    """The organisation's rules for a place on one of its teams, checked before the lead side
    puts anyone on a team: a live project, an active volunteer of the organisation, and the
    organisation's own onboarding finished."""
    if info["state"] != "published":
        raise services.Conflict("This project is not active any more.")
    if not teams.in_standing(conn, volunteer_id, info["org_id"]):
        raise services.Invalid(
            "They are not an active volunteer of this organisation at the moment."
        )
    if services.onboarding.pending_with_org(conn, volunteer_id, info["org_id"]):
        raise services.Invalid(
            f"They have not finished onboarding with {info['org_name']} yet. That comes first."
        )


def _lead_decision(
    conn: sqlite3.Connection, project_id: str, volunteer_id: str, *, from_state: str,
    to_state: str, actor_id: str, note: str = "",
) -> None:  # fmt: skip
    """Move a placement from one state to another on the lead side's say, or Conflict."""
    changed = conn.execute(
        "UPDATE placements SET state = ?, lead_actor_id = ?, lead_note = ?, "
        "version = version + 1, updated_at = ? "
        "WHERE project_id = ? AND volunteer_id = ? AND state = ?",
        (to_state, actor_id, note, now(), project_id, volunteer_id, from_state),
    ).rowcount
    if not changed:
        known = conn.execute(
            "SELECT 1 FROM placements WHERE project_id = ? AND volunteer_id = ?",
            (project_id, volunteer_id),
        ).fetchone()
        if known is None:
            raise services.NotFound("placement")
        raise services.Conflict("Their place on this team has changed. Reload and try again.")
    enqueue(conn, "project", project_id)  # ON_TEAM in Relate follows the state


def accept(db: Database, actor_id: str, project_id: str, volunteer_id: str) -> None:
    """The lead side accepts a volunteer who has finished the project's onboarding steps: they
    are on the team from here. Steps still open must be finished (or confirmed for them) first."""
    with db.tx() as conn:
        info = _check_lead(conn, actor_id, project_id, volunteer_id)
        _may_hold_place(conn, info, volunteer_id)
        name = _name(conn, volunteer_id)
        left = services.onboarding.steps_left(conn, volunteer_id, info["org_id"], project_id)
        if left:
            raise services.Invalid(
                f"{name} still has {left} onboarding step{'s' if left != 1 else ''} to finish "
                "for this project."
            )
        _lead_decision(conn, project_id, volunteer_id, from_state="onboarding", to_state="team",
                       actor_id=actor_id)  # fmt: skip
        teams.record(conn, project_id, volunteer_id, "accepted", actor_id=actor_id)
        notify(conn, volunteer_id, "placement",
               f"You have been accepted onto the team for “{info['title']}”.",
               "/opportunities")  # fmt: skip
        teams.notify_deciders(
            conn, info["org_id"], info["lead_id"], f"{name} is now on the team for "
            f"“{info['title']}”.", teams.project_link(info["org_id"], project_id),
            exclude=(volunteer_id, actor_id),
        )  # fmt: skip


def suspend(
    db: Database, actor_id: str, project_id: str, volunteer_id: str, *, reason: str = ""
) -> None:
    """The lead side suspends a team member: off the team, with their place kept, until they
    are reinstated or removed. The project's lead cannot be suspended from their own team; an
    team coordinator assigns a new lead first."""
    reason = services.require_reason(reason)
    with db.tx() as conn:
        info = _check_lead(conn, actor_id, project_id, volunteer_id)
        if volunteer_id == actor_id:
            raise services.Forbidden("Someone else must decide on your own place on a team.")
        if volunteer_id == info["lead_id"]:
            raise services.Invalid(
                "They lead this project. A team coordinator assigns a new lead before "
                "suspending them."
            )
        _lead_decision(conn, project_id, volunteer_id, from_state="team", to_state="suspended",
                       actor_id=actor_id, note=reason)  # fmt: skip
        teams.record(conn, project_id, volunteer_id, "suspended", actor_id=actor_id, note=reason)
        notify(conn, volunteer_id, "placement",
               f"You have been suspended from the team for “{info['title']}”: {reason}",
               "/opportunities")  # fmt: skip
        teams.notify_deciders(
            conn, info["org_id"], info["lead_id"], f"{_name(conn, volunteer_id)} has been "
            f"suspended from the team for “{info['title']}”.",
            teams.project_link(info["org_id"], project_id), exclude=(volunteer_id, actor_id),
        )  # fmt: skip


def reinstate(db: Database, actor_id: str, project_id: str, volunteer_id: str) -> None:
    """The lead side puts a suspended team member back on the team, if the organisation's rules
    still allow them a place."""
    with db.tx() as conn:
        info = _check_lead(conn, actor_id, project_id, volunteer_id)
        _may_hold_place(conn, info, volunteer_id)
        _lead_decision(conn, project_id, volunteer_id, from_state="suspended", to_state="team",
                       actor_id=actor_id)  # fmt: skip
        teams.record(conn, project_id, volunteer_id, "reinstated", actor_id=actor_id)
        notify(conn, volunteer_id, "placement",
               f"You are back on the team for “{info['title']}”.", "/opportunities")  # fmt: skip
        teams.notify_deciders(
            conn, info["org_id"], info["lead_id"], f"{_name(conn, volunteer_id)} is back on "
            f"the team for “{info['title']}”.", teams.project_link(info["org_id"], project_id),
            exclude=(volunteer_id, actor_id),
        )  # fmt: skip


def end_team_membership(
    db: Database, actor_id: str, project_id: str, volunteer_id: str, *, reason: str = ""
) -> None:
    """The volunteer leaves, or the lead or an org admin removes them from the team. It ends a
    place on the team, one they were still onboarding for, and one they were suspended from."""
    leaving = actor_id == volunteer_id
    if not leaving:
        reason = services.require_reason(reason)
    with db.tx() as conn:
        if not leaving:  # authorised in the same transaction as the removal
            _check_lead(conn, actor_id, project_id)
        updated = conn.execute(
            "UPDATE placements SET state = ?, version = version + 1, updated_at = ? "
            "WHERE project_id = ? AND volunteer_id = ? "
            "AND state IN ('onboarding', 'team', 'suspended')",
            ("left" if leaving else "removed", now(), project_id, volunteer_id),
        ).rowcount
        if not updated:
            raise services.NotFound("team member")
        teams.end_project_run(conn, volunteer_id, project_id)
        info = _project_info(conn, project_id)
        teams.record(conn, project_id, volunteer_id, "left" if leaving else "removed",
                     actor_id=actor_id, note="" if leaving else reason)  # fmt: skip
        if leaving:
            name = conn.execute(
                "SELECT display_name FROM users WHERE id = ?", (volunteer_id,)
            ).fetchone()[0]
            notify(conn, volunteer_id, "placement", f"You left the team for “{info['title']}”.",
                   "/opportunities")  # fmt: skip
            teams.notify_deciders(
                conn, info["org_id"], info["lead_id"],
                f"{name} left the team for “{info['title']}”.",
                teams.project_link(info["org_id"], project_id), exclude=(volunteer_id,),
            )  # fmt: skip
        else:
            notify(conn, volunteer_id, "placement",
                   f"You were removed from the team for “{info['title']}”: {reason}",
                   "/opportunities")  # fmt: skip
        enqueue(conn, "project", project_id)


def for_volunteer(db: Database, user_id: str) -> list[dict]:
    """My invitations, teams and places that ended. A match the lead has not acted on is not
    listed: being a candidate asks nothing of the volunteer (their matches page shows the fit).
    Neither is a place on a project that was completed: finished work is their history
    (app.services.engagements.my_history), with how it went."""
    with db.read() as conn:
        rows = conn.execute(
            "SELECT p.*, s.title, o.name AS org_name, s.org_id FROM placements p "
            "JOIN submission_current s ON s.id = p.project_id "
            "JOIN organisations o ON o.id = s.org_id WHERE p.volunteer_id = ? "
            "AND CASE p.state WHEN 'eligible' THEN p.lead_said = 'yes' WHEN 'team' THEN 1 "
            "    WHEN 'completed' THEN 0 ELSE p.invited_at IS NOT NULL END "
            "ORDER BY CASE p.state WHEN 'eligible' THEN 0 WHEN 'onboarding' THEN 1 "
            "WHEN 'team' THEN 2 WHEN 'suspended' THEN 3 ELSE 4 END, "
            "p.updated_at DESC",
            (user_id,),
        ).fetchall()
    return [_shape(r) | {"state_label": _volunteer_label(r)} for r in rows]


def for_project(db: Database, actor_id: str, project_id: str) -> list[dict]:
    """Candidates and team for the lead side (display names, no emails). Candidates come in
    the matching order: preferred cause, required skills covered, optional skills offered."""
    with db.read() as conn:
        return _for_project(conn, actor_id, project_id)


def lead_side(db: Database, actor_id: str, project_id: str) -> dict:
    """Everything the lead side works with on a project's page, read together: the candidates
    and team, the project's own onboarding workflow, and where each volunteer is in it."""
    with db.read() as conn:
        team = _for_project(conn, actor_id, project_id)
        org_id = _project_info(conn, project_id)["org_id"]
        return {
            "team": team,
            "flow": services.onboarding.load_workflow(conn, actor_id, org_id, project_id),
            "progress": services.onboarding.load_progress(conn, actor_id, org_id, project_id),
        }


def _for_project(conn: sqlite3.Connection, actor_id: str, project_id: str) -> list[dict]:
    info = _check_lead(conn, actor_id, project_id)
    rows = conn.execute(
        "SELECT p.*, u.display_name, d.display_name AS decided_by FROM placements p "
        "JOIN users u ON u.id = p.volunteer_id LEFT JOIN users d ON d.id = p.lead_actor_id "
        "WHERE p.project_id = ? ORDER BY CASE p.state WHEN 'eligible' THEN 0 "
        "WHEN 'onboarding' THEN 1 WHEN 'team' THEN 2 WHEN 'suspended' THEN 3 ELSE 4 END, "
        "p.preferred DESC, "
        "json_array_length(p.matched_skills) DESC, json_array_length(p.optional_skills) DESC, "
        "u.display_name, u.id",
        (project_id,),
    ).fetchall()
    left = {
        r["volunteer_id"]: services.onboarding.steps_left(
            conn, r["volunteer_id"], info["org_id"], project_id
        )
        for r in rows
        if r["state"] == "onboarding"
    }
    return [
        _shape(r)
        | {
            # Still onboarding: how many of the project's steps they have left (0: ready to
            # be accepted). None for everyone else.
            "steps_left": left.get(r["volunteer_id"]),
            "is_lead": r["volunteer_id"] == info["lead_id"],
            # Nobody decides on their own place (the lead's own is automatic).
            "can_decide": r["volunteer_id"] != actor_id or info["lead_id"] == actor_id,
        }
        for r in rows
    ]


def states_for(conn: sqlite3.Connection, *, volunteer_id: str = "", project_ids=()) -> dict:
    """Short status of placements, for the matches and coverage screens: keyed by project for
    one volunteer, or by (project, volunteer) for a set of projects."""
    if volunteer_id:
        rows = conn.execute(
            "SELECT project_id, volunteer_id, state, lead_said FROM placements "
            "WHERE volunteer_id = ?",
            (volunteer_id,),
        ).fetchall()
        return {r["project_id"]: _status(r) for r in rows}
    ids = list(project_ids)
    if not ids:
        return {}
    rows = conn.execute(
        "SELECT project_id, volunteer_id, state, lead_said FROM placements "
        f"WHERE project_id IN ({_marks(ids)})",
        ids,
    ).fetchall()
    return {(r["project_id"], r["volunteer_id"]): _status(r) for r in rows}


def _status(row: sqlite3.Row) -> str:
    if row["state"] == "eligible":
        return "invited" if row["lead_said"] == "yes" else "candidate"
    return row["state"]


STATUS_LABELS = STATE_LABELS | {"invited": "Invited", "candidate": "Candidate"}


def _volunteer_label(row: sqlite3.Row) -> str:
    if row["state"] == "eligible":
        return "Invited: awaiting your answer"
    return STATE_LABELS[row["state"]]


def _shape(row: sqlite3.Row) -> dict:
    data = dict(row)
    names = get_catalogue()
    data["matched_skills"] = [names.skill_name(s) for s in json.loads(row["matched_skills"])]
    data["optional_skills"] = [names.skill_name(s) for s in json.loads(row["optional_skills"])]
    data["preferred"] = bool(row["preferred"])
    invited = row["state"] == "eligible" and row["lead_said"] == "yes"
    data["status"] = _status(row)
    data["state_label"] = INVITED_LABEL if invited else STATE_LABELS[row["state"]]
    return data
