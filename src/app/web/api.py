"""The JSON API: every action in the system is available here, under /api/v1.

The mobile companion and the Streamlit admin console use only this API. The server-rendered web
pages call the same service functions in-process, so the rules (roles, onboarding states,
versions) are enforced in one place, app.services / app.placements, whichever way in.

Bearer tokens only: the API never reads the web session cookie, so cross-site form posts cannot
ride on it and no CSRF token is needed. Tokens are opaque, revocable sessions (kind 'bearer').
Roles: system `admin` (all access), system `moderator` (vets volunteers), per-organisation
`org_admin` (its projects and volunteers, reviews), `reviewer` (reviews suggestions) and
`team_coordinator` (assigns project leads, decides who joins the teams), plus the project lead for
their own project. Errors are JSON: 401 signed out, 403 role missing, 404 unknown
or not visible, 409 stale version or wrong state, 422 invalid input.

Interactive docs: /api/docs (OpenAPI at /api/openapi.json).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app import audit, auth, matching, notify, placements, profile_view, services
from app.graph import cypher
from app.web import schemas as m
from app.web.deps import drain, require_signup

router = APIRouter(prefix="/api/v1")
_bearer = HTTPBearer(auto_error=False)


# ------------------------------------------------------------------ plumbing
def _rt(request: Request):
    return request.app.state.rt


def bearer_session(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> auth.Session:
    session = auth.load_session(
        _rt(request).db, creds.credentials if creds else None, kind="bearer"
    )
    if session is None:
        raise HTTPException(401, "Sign in first", headers={"WWW-Authenticate": "Bearer"})
    request.state.session = session  # the audit middleware reads who made the request
    return session


Api = Annotated[auth.Session, Depends(bearer_session)]


# ------------------------------------------------------------------ auth
@router.post("/auth/signup", status_code=201, tags=["auth"], dependencies=[Depends(require_signup)])
def sign_up(body: m.SignUp, request: Request) -> m.SignedUp:
    """Create an account (status `draft`) and sign in. Grants no role and joins nothing.

    403 while the deployment has `SIGNUP_ENABLED` off."""
    rt = _rt(request)
    try:
        user_id = auth.signup(
            rt.db, email=body.email, display_name=body.display_name, password=body.password
        )
    except auth.AuthError as error:
        raise HTTPException(422, str(error)) from error
    token = auth.create_session(
        rt.db, user_id, kind="bearer", ttl_hours=rt.settings.session_ttl_hours
    )
    return {"token": token, "user_id": user_id}


@router.post("/session", tags=["auth"])
def sign_in(body: m.SignIn, request: Request) -> m.Token:
    rt = _rt(request)
    user_id = auth.authenticate(rt.db, email=body.email, password=body.password)
    audit.record_now(
        rt.db, "auth.sign_in", actor_id=user_id, outcome="ok" if user_id else "denied",
        source="api",
    )  # fmt: skip
    if user_id is None:
        raise HTTPException(401, "Wrong email or password")
    token = auth.create_session(
        rt.db, user_id, kind="bearer", ttl_hours=rt.settings.session_ttl_hours
    )
    return {"token": token}


@router.delete("/session", status_code=204, tags=["auth"])
def sign_out(request: Request, session: Api) -> None:
    auth.revoke_session(_rt(request).db, session.token_hash)


# ------------------------------------------------------------------ me
@router.get("/me", tags=["me"])
def me(request: Request, session: Api) -> m.Me:
    """Who I am: onboarding status, roles, organisations and unread notifications."""
    rt = _rt(request)
    with rt.db.read() as conn:
        actor = services.load_actor(conn, session.user_id)
        orgs = services.load_directory(conn, session.user_id, admin=actor.admin)
        unread = notify.count_unread(conn, session.user_id)
    accessible = services.accessible(orgs)
    active = (
        session.active_org_id if any(o["id"] == session.active_org_id for o in accessible) else None
    )
    return {
        "user": {"id": session.user_id, "display_name": session.display_name},
        "status": actor.status,
        "status_label": services.VOLUNTEER_STATUS_LABELS[actor.status],
        "system_roles": sorted(actor.system_roles),
        "organisations": orgs,
        "active_org_id": active or (accessible[0]["id"] if accessible else None),
        "unread_notifications": unread,
    }


@router.put("/me/active-org", tags=["me"])
def set_active(body: m.ActiveOrg, request: Request, session: Api) -> m.ActiveOrgOut:
    """Switch the organisation I'm looking at. Never grants access."""
    rt = _rt(request)
    services.require_access(rt.db, session.user_id, body.org_id)
    auth.set_active_org(rt.db, session.token_hash, body.org_id)
    return {"active_org_id": body.org_id}


def _profile(rt, user_id: str) -> dict:
    actor = services.get_actor(rt.db, user_id)
    return profile_view.overview(rt.db, rt.graph, user_id) | {
        "status": actor.status,
        "status_label": services.VOLUNTEER_STATUS_LABELS[actor.status],
        "can_submit_for_vetting": actor.status in services.SUBMITTABLE,
    }


@router.get("/profile", tags=["me"])
def get_profile(request: Request, session: Api) -> m.Profile:
    """My skills, cause stances, onboarding status, and active and past projects."""
    return _profile(_rt(request), session.user_id)


@router.put("/profile", tags=["me"])
def put_profile(body: m.ProfileIn, request: Request, session: Api) -> m.Profile:
    """Replace my profile; `version` guards against stale edits."""
    rt = _rt(request)
    services.update_profile(
        rt.db, session.user_id,
        display_name=body.display_name, skills=body.skills, causes=body.causes,
        expected_version=body.version,
    )  # fmt: skip
    drain(rt)
    return _profile(rt, session.user_id)


@router.post("/me/vetting", tags=["me"])
def submit_for_vetting(request: Request, session: Api) -> m.StatusOut:
    """Onboarding step 1: send my completed profile for screening by a moderator."""
    services.submit_for_vetting(_rt(request).db, session.user_id)
    return {"status": "screening"}


@router.get("/notifications", tags=["me"])
def notifications(request: Request, session: Api, unread: bool = False) -> list[m.Notification]:
    return notify.list_for(_rt(request).db, session.user_id, unread_only=unread)


@router.post("/notifications/{notification_id}/read", tags=["me"])
def read_notification(notification_id: int, request: Request, session: Api) -> m.ReadOut:
    if not notify.mark_read(_rt(request).db, session.user_id, notification_id):
        raise HTTPException(404, "Not found")
    return {"read": 1}


@router.post("/notifications/read-all", tags=["me"])
def read_all_notifications(request: Request, session: Api) -> m.ReadOut:
    return {"read": notify.mark_read(_rt(request).db, session.user_id)}


# ------------------------------------------------------------------ organisations
@router.get("/orgs", tags=["organisations"])
def organisations(request: Request, session: Api) -> list[m.Organisation]:
    return services.directory(_rt(request).db, session.user_id)


@router.put("/orgs/{org_id}/membership", tags=["organisations"])
def join(org_id: str, request: Request, session: Api) -> m.MembershipOut:
    """Opt in as a volunteer. Matching starts once my account is active (vetted)."""
    rt = _rt(request)
    services.set_membership(rt.db, session.user_id, org_id, opted_in=True)
    drain(rt)
    return {"opted_in": True}


@router.delete("/orgs/{org_id}/membership", tags=["organisations"])
def leave(org_id: str, request: Request, session: Api) -> m.MembershipOut:
    rt = _rt(request)
    services.set_membership(rt.db, session.user_id, org_id, opted_in=False)
    drain(rt)
    return {"opted_in": False}


@router.get("/orgs/{org_id}/matches", tags=["organisations"])
def org_matches(
    org_id: str, request: Request, session: Api, preferred_only: bool = False
) -> m.Matches:
    rt = _rt(request)
    result = matching.volunteer_matches(
        rt.db, rt.graph, session.user_id, [org_id], preferred_only=preferred_only
    )
    return {
        "withheld": result.withheld.reason if result.withheld else None,
        "matches": [
            {
                "project": m["project"],
                "title": m["title"],
                "organisation": m["org_name"],
                "cause": m["cause_name"],
                "preferred_cause": m["preferred"],
                "matched_skills": m["matched_names"],
                "optional_skills": m["optional_names"],
                "remaining_skills": m["remaining_names"],
                "placement": m["placement"],
                "placement_label": m["placement_label"],
            }
            for m in result.matches
        ],
        "excluded": [
            {"project": e["project"], "title": e["title"], "reasons": e["reasons"]}
            for e in result.excluded
        ],
    }


@router.get("/orgs/{org_id}/projects", tags=["organisations"])
def org_projects(org_id: str, request: Request, session: Api) -> list[m.Project]:
    """Active (published) projects of an organisation I belong to."""
    rt = _rt(request)
    return [
        {
            "project": r["project"],
            "title": r["title"],
            "summary": r["summary"],
            "cause": r["cause_name"],
            "required_skills": r["required_names"],
            "optional_skills": r["optional_names"],
        }
        for r in matching.browse(rt.db, rt.graph, session.user_id, org_id)
    ]


@router.get("/orgs/{org_id}/coverage", tags=["organisations"])
def org_coverage(org_id: str, request: Request, session: Api) -> m.Coverage:
    """Candidates per required skill and coverage gaps (reviewers and org admins)."""
    rt = _rt(request)
    cov = matching.project_coverage(rt.db, rt.graph, session.user_id, org_id)
    return {
        "withheld": cov.withheld.reason if cov.withheld else None,
        "projects": [
            {
                "project": p["project"],
                "title": p["title"],
                "gaps": p["gaps"],
                "team_gaps": p["team_gaps"],
                "per_skill": [
                    {
                        "skill": row["skill"],
                        "team": row["team"],
                        "candidates": [
                            {
                                "volunteer": c["volunteer"],
                                "name": c["name"],
                                "preferred_cause": c["preferred"],
                                "status": c["status"],
                                "status_label": c["status_label"],
                            }  # fmt: skip
                            for c in row["candidates"]
                        ],
                        "close": [
                            {"volunteer": c["volunteer"], "name": c["name"], "via": c["via_names"]}
                            for c in row["close"]
                        ],
                    }
                    for row in p["per_skill"]
                ],
            }
            for p in cov.projects
        ],
    }


@router.get("/orgs/{org_id}/volunteers", tags=["organisations"])
def org_volunteers(org_id: str, request: Request, session: Api) -> list[m.OrgVolunteer]:
    """The organisation's opted-in volunteers (org admins). No contact details."""
    return services.org_volunteers(_rt(request).db, session.user_id, org_id)


@router.post("/orgs/{org_id}/volunteers/{user_id}/remove", tags=["organisations"])
def remove_volunteer(
    org_id: str, user_id: str, body: m.ReasonIn, request: Request, session: Api
) -> m.RemovedOut:
    """An org admin removes a volunteer from the organisation and its project teams."""
    rt = _rt(request)
    services.remove_volunteer(rt.db, session.user_id, org_id, user_id, reason=body.reason)
    drain(rt)
    return {"removed": True}


# ------------------------------------------------------------------ project onboarding
@router.get("/submissions", tags=["projects"])
def my_submissions(request: Request, session: Api) -> list[m.SubmissionSummary]:
    return [
        s | {"state_label": services.PROJECT_STATES[s["state"]]}
        for s in services.my_submissions(_rt(request).db, session.user_id)
    ]


@router.post("/submissions", status_code=201, tags=["projects"])
def create_submission(body: m.SubmissionIn, request: Request, session: Api) -> m.SubmissionCreated:
    """Project onboarding step 1: suggest a project (active accounts only)."""
    sid = services.create_submission(_rt(request).db, session.user_id, body.org_id, body.draft())
    return {"id": sid, "state": "submitted"}


@router.post("/submissions/drafts", status_code=201, tags=["projects"])
def save_draft(body: m.DraftSaveIn, request: Request, session: Api) -> m.SubmissionCreated:
    """Save a suggestion as a draft: private to its author and not yet sent for review. Only
    the title and the organisation are required at this point."""
    sid = services.save_draft(_rt(request).db, session.user_id, body.org_id, body.draft())
    return {"id": sid, "state": "draft"}


@router.put("/submissions/{sid}/draft", tags=["projects"])
def update_draft(
    sid: str, body: m.DraftUpdateIn, request: Request, session: Api
) -> m.SubmissionStateOut:
    """Save my draft again, in place. The organisation may still be changed."""
    services.update_draft(
        _rt(request).db, session.user_id, sid, body.org_id, body.draft(),
        expected_version=body.version,
    )  # fmt: skip
    return {"state": "draft"}


@router.post("/submissions/{sid}/submit", tags=["projects"])
def submit_draft(
    sid: str, body: m.VersionIn, request: Request, session: Api
) -> m.SubmissionStateOut:
    """Send my draft for review. It must be complete by now."""
    services.submit_draft(_rt(request).db, session.user_id, sid, expected_version=body.version)
    return {"state": "submitted"}


@router.delete("/submissions/{sid}", tags=["projects"])
def discard_draft(sid: str, version: int, request: Request, session: Api) -> m.RemovedOut:
    """Delete my draft. Suggestions that were submitted cannot be deleted."""
    services.discard_draft(_rt(request).db, session.user_id, sid, expected_version=version)
    return {"removed": True}


@router.get("/submissions/{sid}", tags=["projects"])
def get_submission(sid: str, request: Request, session: Api) -> m.Submission:
    """Visible to the author and to the organisation's reviewers and admins."""
    return services.get_submission(_rt(request).db, session.user_id, sid)


@router.post("/submissions/{sid}/revisions", tags=["projects"])
def revise_submission(
    sid: str, body: m.RevisionIn, request: Request, session: Api
) -> m.SubmissionStateOut:
    """Scoping: the author adds the requested detail and resubmits."""
    services.revise_submission(
        _rt(request).db, session.user_id, sid, body.draft(), expected_version=body.version
    )
    return {"state": "submitted"}


@router.get("/orgs/{org_id}/submissions", tags=["projects"])
def review_queue(org_id: str, request: Request, session: Api) -> list[m.QueueItem]:
    return services.review_queue(_rt(request).db, session.user_id, org_id)


@router.post("/orgs/{org_id}/submissions/{sid}/decision", tags=["projects"])
def decide(
    org_id: str, sid: str, body: m.DecisionIn, request: Request, session: Api
) -> m.SubmissionStateOut:
    """Review: request scoping detail, reject, or approve (activates once in Relate)."""
    rt = _rt(request)
    services.review_submission(
        rt.db, session.user_id, org_id, sid,
        action=body.action, reason=body.reason, private_note=body.private_note,
        revision=body.revision, expected_version=body.version,
    )  # fmt: skip
    drain(rt)
    return {"state": services.get_submission(rt.db, session.user_id, sid)["state"]}


@router.post("/orgs/{org_id}/projects/{sid}/withdraw", tags=["projects"])
def withdraw(
    org_id: str, sid: str, body: m.WithdrawIn, request: Request, session: Api
) -> m.SubmissionStateOut:
    rt = _rt(request)
    services.withdraw_project(
        rt.db, session.user_id, org_id, sid, reason=body.reason, expected_version=body.version
    )
    drain(rt)
    return {"state": "withdrawn"}


@router.put("/orgs/{org_id}/projects/{sid}/lead", tags=["projects"])
def set_lead(org_id: str, sid: str, body: m.LeadIn, request: Request, session: Api) -> m.LeadOut:
    rt = _rt(request)
    services.set_project_lead(rt.db, session.user_id, org_id, sid, body.user_id)
    drain(rt)  # the new lead joins the team with the projection
    return {"lead_id": body.user_id}


# ------------------------------------------------------------------ placements (volunteer step 2)
@router.get("/placements", tags=["placements"])
def my_placements(request: Request, session: Api) -> list[m.MyPlacement]:
    """My eligible offers, teams and history."""
    return placements.for_volunteer(_rt(request).db, session.user_id)


@router.post("/placements/{project_id}/answer", tags=["placements"])
def answer_as_volunteer(
    project_id: str, body: m.AnswerIn, request: Request, session: Api
) -> m.PlacementStateOut:
    rt = _rt(request)
    state = placements.volunteer_respond(
        rt.db, session.user_id, project_id, body.answer, version=body.version
    )
    drain(rt)
    return {"state": state}


@router.post("/placements/{project_id}/leave", tags=["placements"])
def leave_team(project_id: str, request: Request, session: Api) -> m.PlacementStateOut:
    rt = _rt(request)
    placements.end_team_membership(rt.db, session.user_id, project_id, session.user_id)
    drain(rt)
    return {"state": "left"}


@router.get("/projects/{project_id}/placements", tags=["placements"])
def project_placements(project_id: str, request: Request, session: Api) -> list[m.ProjectPlacement]:
    """Candidates and team, for the project lead and org admins."""
    return placements.for_project(_rt(request).db, session.user_id, project_id)


@router.post("/projects/{project_id}/placements/{volunteer_id}/answer", tags=["placements"])
def answer_as_lead(
    project_id: str, volunteer_id: str, body: m.AnswerIn, request: Request, session: Api
) -> m.PlacementStateOut:
    rt = _rt(request)
    state = placements.lead_respond(
        rt.db, session.user_id, project_id, volunteer_id, body.answer, version=body.version,
        note=body.reason,
    )  # fmt: skip
    drain(rt)
    return {"state": state}


@router.post("/projects/{project_id}/placements/{volunteer_id}/reopen", tags=["placements"])
def reopen_placement(
    project_id: str, volunteer_id: str, request: Request, session: Api
) -> m.PlacementStateOut:
    """The lead side reopens a declined, left or removed placement. The volunteer becomes a
    candidate again if they still match; the returned state says whether they did."""
    rt = _rt(request)
    placements.reopen(rt.db, session.user_id, project_id, volunteer_id)
    drain(rt)
    return _placement_state(rt, project_id, volunteer_id)


def _placement_state(rt, project_id: str, volunteer_id: str) -> dict:
    with rt.db.read() as conn:
        state = conn.execute(
            "SELECT state FROM placements WHERE project_id = ? AND volunteer_id = ?",
            (project_id, volunteer_id),
        ).fetchone()[0]
    return {"state": state}


@router.post("/projects/{project_id}/placements/{volunteer_id}/accept", tags=["placements"])
def accept_onto_team(
    project_id: str, volunteer_id: str, request: Request, session: Api
) -> m.PlacementStateOut:
    """The project lead or an org admin accepts a volunteer who has finished the project's
    onboarding steps onto the team."""
    rt = _rt(request)
    placements.accept(rt.db, session.user_id, project_id, volunteer_id)
    drain(rt)
    return _placement_state(rt, project_id, volunteer_id)


@router.post("/projects/{project_id}/placements/{volunteer_id}/suspend", tags=["placements"])
def suspend_from_team(
    project_id: str, volunteer_id: str, body: m.ReasonIn, request: Request, session: Api
) -> m.PlacementStateOut:
    """The project lead or an org admin suspends a team member, with a reason they are shown.
    Their place is kept until they are reinstated or removed."""
    rt = _rt(request)
    placements.suspend(rt.db, session.user_id, project_id, volunteer_id, reason=body.reason)
    drain(rt)
    return _placement_state(rt, project_id, volunteer_id)


@router.post("/projects/{project_id}/placements/{volunteer_id}/reinstate", tags=["placements"])
def reinstate_on_team(
    project_id: str, volunteer_id: str, request: Request, session: Api
) -> m.PlacementStateOut:
    """The project lead or an org admin puts a suspended team member back on the team."""
    rt = _rt(request)
    placements.reinstate(rt.db, session.user_id, project_id, volunteer_id)
    drain(rt)
    return _placement_state(rt, project_id, volunteer_id)


@router.post("/projects/{project_id}/placements/{volunteer_id}/remove", tags=["placements"])
def remove_from_team(
    project_id: str, volunteer_id: str, body: m.ReasonIn, request: Request, session: Api
) -> m.PlacementStateOut:
    rt = _rt(request)
    placements.end_team_membership(
        rt.db, session.user_id, project_id, volunteer_id, reason=body.reason
    )
    drain(rt)
    return {"state": "removed"}


# ------------------------------------------------------------------ moderation (vetting)
@router.get("/moderation/volunteers", tags=["moderation"])
def vetting_queue(
    request: Request, session: Api, status: str = "screening"
) -> list[m.VettingQueueItem]:
    """Volunteers in a given onboarding status (moderators and admins)."""
    return services.vetting_queue(_rt(request).db, session.user_id, status)


@router.get(
    "/moderation/volunteers/{user_id}", tags=["moderation"], response_model_exclude_unset=True
)
def vetting_record(user_id: str, request: Request, session: Api) -> m.VettingRecord:
    return services.vetting_record(_rt(request).db, session.user_id, user_id)


@router.post("/moderation/volunteers/{user_id}/decision", tags=["moderation"])
def vet(user_id: str, body: m.VetIn, request: Request, session: Api) -> m.StatusOut:
    """Approve, request more information, decline, suspend or reinstate a volunteer."""
    rt = _rt(request)
    status = services.vet_volunteer(
        rt.db, session.user_id, user_id, action=body.action, note=body.note,
        private_note=body.private_note, expected_version=body.version,
    )  # fmt: skip
    drain(rt)
    return {"status": status}


# ------------------------------------------------------------------ system admin
@router.get("/admin/users", tags=["admin"])
def admin_users(request: Request, session: Api) -> list[m.AdminUser]:
    return services.list_users(_rt(request).db, session.user_id)


@router.put("/admin/users/{user_id}/system-roles/{role}", tags=["admin"])
def grant_system_role(user_id: str, role: str, request: Request, session: Api) -> m.Granted:
    services.set_system_role(_rt(request).db, session.user_id, user_id, role, granted=True)
    return {"granted": role}


@router.delete("/admin/users/{user_id}/system-roles/{role}", tags=["admin"])
def revoke_system_role(user_id: str, role: str, request: Request, session: Api) -> m.Revoked:
    services.set_system_role(_rt(request).db, session.user_id, user_id, role, granted=False)
    return {"revoked": role}


@router.put("/admin/orgs/{org_id}/roles/{user_id}/{role}", tags=["admin"])
def grant_org_role(
    org_id: str, user_id: str, role: str, request: Request, session: Api
) -> m.Granted:
    services.set_org_role(_rt(request).db, session.user_id, org_id, user_id, role, granted=True)
    return {"granted": role}


@router.delete("/admin/orgs/{org_id}/roles/{user_id}/{role}", tags=["admin"])
def revoke_org_role(
    org_id: str, user_id: str, role: str, request: Request, session: Api
) -> m.Revoked:
    services.set_org_role(_rt(request).db, session.user_id, org_id, user_id, role, granted=False)
    return {"revoked": role}


@router.post("/admin/orgs", status_code=201, tags=["admin"])
def create_org(body: m.OrganisationIn, request: Request, session: Api) -> m.Created:
    rt = _rt(request)
    org_id = services.create_organisation(
        rt.db, session.user_id, key=body.key, name=body.name, description=body.description
    )
    drain(rt)
    return {"id": org_id}


@router.get("/admin/projection", tags=["admin"])
def projection_status(request: Request, session: Api) -> m.ProjectionStatus:
    rt = _rt(request)
    services.require_admin(rt.db, session.user_id)
    return rt.projector.status().__dict__


@router.post("/admin/projection/replay", tags=["admin"])
def projection_replay(request: Request, session: Api) -> m.Replayed:
    rt = _rt(request)
    services.require_admin(rt.db, session.user_id)
    result = rt.projector.drain(ignore_backoff=True)
    return {"done": result.done, "failed": result.failed}


@router.get("/admin/graph", tags=["admin"])
def graph_overview(request: Request, session: Api) -> m.GraphOverview:
    """Label and relationship counts plus the org → project → skill → volunteer inspection."""
    rt = _rt(request)
    services.require_admin(rt.db, session.user_id)
    with rt.graph.reader() as tx:
        labels = tx.read(cypher.COUNT_LABELS)
        rels = tx.read(cypher.COUNT_RELATIONSHIPS)
        inspect = tx.read(cypher.INSPECT)
    with rt.db.read() as conn:
        names = dict(conn.execute("SELECT id, display_name FROM users").fetchall())
    for row in inspect:
        row["eligible_volunteers"] = [names.get(v, v) for v in row["eligible_volunteers"]]
    return {"labels": labels, "relationships": rels, "inspect": inspect}
