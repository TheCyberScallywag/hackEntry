"""Published projects, matches and coverage, and placements: the lead invites, the volunteer
answers."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app import matching, placements, services
from app.web.deps import (
    RT,
    Org,
    User,
    UserForm,
    active_org,
    drain,
    nav_for,
    redirect,
    render,
    to_int,
)

router = APIRouter(include_in_schema=False, default_response_class=HTMLResponse)


# ---------------------------------------------------------------- projects + matches
@router.get("/o/{org_id}/projects")
def projects(org: Org, request: Request, rt: RT, session: User):
    rows = matching.browse(rt.db, rt.graph, session.user_id, org["id"])
    return render(request, "projects.html", session, org=org, projects=rows)


@router.get("/o/{org_id}/projects/{project_id}")
def project_detail(org: Org, project_id: str, request: Request, rt: RT, session: User):
    org_id, user_id = org["id"], session.user_id
    access = services.require_access(rt.db, user_id, org_id)
    project = matching.project(rt.db, rt.graph, user_id, org_id, project_id)
    is_lead = project["sql"]["lead_id"] == user_id
    coverage = mine = team = None
    if access.org_admin or is_lead:
        team = placements.for_project(rt.db, user_id, project_id)
    if access.reviewer:
        coverage = matching.project_coverage(rt.db, rt.graph, user_id, org_id, project_id)
    if access.volunteer:
        mine = matching.volunteer_matches(rt.db, rt.graph, user_id, [org_id])
    return render(
        request, "project.html", session, org=org, project=project, access=access,
        coverage=coverage, mine=mine, team=team, is_lead=is_lead,
    )  # fmt: skip


@router.get("/matches")
def matches(request: Request, rt: RT, session: User):
    volunteering = nav_for(request, session).volunteering
    scope = request.query_params.get("scope") or (
        (active_org(session, volunteering) or {}).get("id", "all")
    )
    preferred_only = request.query_params.get("preferred_only") == "1"
    org_ids = [o["id"] for o in volunteering] if scope == "all" else [scope]
    result = matching.volunteer_matches(
        rt.db, rt.graph, session.user_id, org_ids, preferred_only=preferred_only
    )
    return render(
        request, "matches.html", session, result=result, scope=scope,
        volunteering=volunteering, preferred_only=preferred_only,
    )  # fmt: skip


@router.get("/o/{org_id}/coverage")
def coverage(org: Org, request: Request, rt: RT, session: User):
    result = matching.project_coverage(rt.db, rt.graph, session.user_id, org["id"])
    return render(request, "coverage.html", session, org=org, coverage=result)


# ---------------------------------------------------------------- placements
@router.get("/opportunities")
def opportunities(request: Request, rt: RT, session: User):
    return render(
        request, "opportunities.html", session,
        items=placements.for_volunteer(rt.db, session.user_id),
    )  # fmt: skip


@router.post("/opportunities/{project_id}/answer")
def answer(project_id: str, rt: RT, session: User, form: UserForm):
    placements.volunteer_respond(
        rt.db, session.user_id, project_id, str(form.get("answer", "")),
        version=to_int(form.get("version")),
    )  # fmt: skip
    drain(rt)
    return redirect("/opportunities", "answered")


@router.post("/opportunities/{project_id}/leave")
def leave_team(project_id: str, rt: RT, session: User, _form: UserForm):
    placements.end_team_membership(rt.db, session.user_id, project_id, session.user_id)
    drain(rt)
    return redirect("/opportunities", "left_team")


@router.post("/o/{org_id}/projects/{project_id}/candidates/{volunteer_id}/answer")
def lead_answer(
    org_id: str, project_id: str, volunteer_id: str, rt: RT, session: User, form: UserForm
):
    answer = str(form.get("answer", ""))
    placements.lead_respond(
        rt.db, session.user_id, project_id, volunteer_id, answer,
        version=to_int(form.get("version")), note=str(form.get("reason", "")),
    )  # fmt: skip
    drain(rt)
    return redirect(
        f"/o/{org_id}/projects/{project_id}", "invited" if answer == "yes" else "answered"
    )


@router.post("/o/{org_id}/projects/{project_id}/candidates/{volunteer_id}/reopen")
def lead_reopen(
    org_id: str, project_id: str, volunteer_id: str, rt: RT, session: User, _form: UserForm
):
    placements.reopen(rt.db, session.user_id, project_id, volunteer_id)
    drain(rt)
    return redirect(f"/o/{org_id}/projects/{project_id}", "reopened")


@router.post("/o/{org_id}/projects/{project_id}/team/{volunteer_id}/remove")
def lead_remove(
    org_id: str, project_id: str, volunteer_id: str, rt: RT, session: User, form: UserForm
):
    placements.end_team_membership(
        rt.db, session.user_id, project_id, volunteer_id, reason=str(form.get("reason", ""))
    )
    drain(rt)
    return redirect(f"/o/{org_id}/projects/{project_id}", "removed")
