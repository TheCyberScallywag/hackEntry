"""Published projects, matches and coverage, and placements: the lead invites, the volunteer
answers. The lead side (the project's lead, whom the team coordinators assign, and the
organisation's team coordinators and admins)
also runs the project's own team onboarding and accepts, suspends or removes team members."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app import matching, placements, services
from app.services import onboarding
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
    coverage = mine = team = flow = progress = people = None
    if access.team_coordinator or is_lead:
        try:
            side = placements.lead_side(rt.db, user_id, project_id)
            team, flow, progress = side["team"], side["flow"], side["progress"]
        except services.Forbidden:
            # A lead who is suspended or has left the organisation no longer acts for the
            # project, but may still read its page like any other member.
            is_lead = False
            team = flow = progress = None
    # Team coordinators (and org admins) assign the lead; the people to choose from are loaded
    # on request.
    if access.team_coordinator and request.query_params.get("assign") == "lead":
        people = services.org_volunteers(rt.db, user_id, org_id)
    if access.reviewer:
        coverage = matching.project_coverage(rt.db, rt.graph, user_id, org_id, project_id)
    if access.volunteer:
        mine = matching.volunteer_matches(rt.db, rt.graph, user_id, [org_id])
    return render(
        request, "project.html", session, org=org, project=project, access=access,
        coverage=coverage, mine=mine, team=team, is_lead=is_lead, flow=flow,
        progress=progress, people=people,
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


@router.post("/o/{org_id}/projects/{project_id}/team/{volunteer_id}/accept")
def lead_accept(
    org_id: str, project_id: str, volunteer_id: str, rt: RT, session: User, _form: UserForm
):
    placements.accept(rt.db, session.user_id, project_id, volunteer_id)
    drain(rt)
    return redirect(f"/o/{org_id}/projects/{project_id}", "accepted")


@router.post("/o/{org_id}/projects/{project_id}/team/{volunteer_id}/suspend")
def lead_suspend(
    org_id: str, project_id: str, volunteer_id: str, rt: RT, session: User, form: UserForm
):
    placements.suspend(
        rt.db, session.user_id, project_id, volunteer_id, reason=str(form.get("reason", ""))
    )
    drain(rt)
    return redirect(f"/o/{org_id}/projects/{project_id}", "suspended")


@router.post("/o/{org_id}/projects/{project_id}/team/{volunteer_id}/reinstate")
def lead_reinstate(
    org_id: str, project_id: str, volunteer_id: str, rt: RT, session: User, _form: UserForm
):
    placements.reinstate(rt.db, session.user_id, project_id, volunteer_id)
    drain(rt)
    return redirect(f"/o/{org_id}/projects/{project_id}", "reinstated")


@router.post("/o/{org_id}/projects/{project_id}/complete")
def complete(org_id: str, project_id: str, rt: RT, session: User, form: UserForm):
    """The lead side marks the project as done; its team's time on it becomes history."""
    services.engagements.complete_project(
        rt.db, session.user_id, org_id, project_id, expected_version=to_int(form.get("version"))
    )
    drain(rt)
    return redirect(f"/o/{org_id}/history", "completed")


@router.get("/o/{org_id}/history")
def history(org: Org, request: Request, rt: RT, session: User):
    """Completed projects the user acts for as lead side, with their teams and ratings."""
    return render(
        request, "org_history.html", session, org=org,
        projects=services.engagements.project_history(rt.db, session.user_id, org["id"]),
        levels=services.engagements.LEVELS,
    )  # fmt: skip


@router.post("/o/{org_id}/history/{engagement_id}/rate")
def rate(org_id: str, engagement_id: str, rt: RT, session: User, form: UserForm):
    skills = {
        key.removeprefix("skill:"): to_int(value)
        for key, value in form.items()
        if key.startswith("skill:") and value
    }
    services.engagements.rate(
        rt.db, session.user_id, engagement_id, readiness=to_int(form.get("readiness")),
        skills=skills, note=str(form.get("note", "")),
        private_note=str(form.get("private_note", "")),
    )  # fmt: skip
    drain(rt)
    return redirect(f"/o/{org_id}/history", "rated")


@router.post("/o/{org_id}/projects/{project_id}/lead")
def assign_lead(org_id: str, project_id: str, rt: RT, session: User, form: UserForm):
    """A team coordinator or org admin hands the project to a lead."""
    services.set_project_lead(
        rt.db, session.user_id, org_id, project_id, str(form.get("lead_id", ""))
    )
    drain(rt)
    return redirect(f"/o/{org_id}/projects/{project_id}", "lead_set")


# ---------------------------------------------------------------- the project's team onboarding
def _flow_saved(rt: RT, org_id: str, project_id: str):
    drain(rt)  # the workflow is held in the graph too
    return redirect(f"/o/{org_id}/projects/{project_id}", "flow_saved")


@router.post("/o/{org_id}/projects/{project_id}/onboarding")
def flow_settings(org_id: str, project_id: str, rt: RT, session: User, form: UserForm):
    onboarding.update_workflow(
        rt.db, session.user_id, org_id, enabled=form.get("enabled") == "1",
        completion_template_id=str(form.get("completion_template_id", "")) or None,
        expected_version=to_int(form.get("version")), project_id=project_id,
    )  # fmt: skip
    return _flow_saved(rt, org_id, project_id)


@router.post("/o/{org_id}/projects/{project_id}/onboarding/steps")
def flow_add_step(org_id: str, project_id: str, rt: RT, session: User, form: UserForm):
    onboarding.add_step(
        rt.db, session.user_id, org_id, title=str(form.get("title", "")),
        description=str(form.get("description", "")),
        completed_by=str(form.get("completed_by", "")),
        email_template_id=str(form.get("email_template_id", "")) or None,
        expected_version=to_int(form.get("version")), project_id=project_id,
    )  # fmt: skip
    return _flow_saved(rt, org_id, project_id)


@router.post("/o/{org_id}/projects/{project_id}/onboarding/steps/{step_id}/move")
def flow_move_step(
    org_id: str, project_id: str, step_id: str, rt: RT, session: User, form: UserForm
):
    onboarding.move_step(
        rt.db, session.user_id, org_id, step_id, direction=str(form.get("direction", "")),
        expected_version=to_int(form.get("version")), project_id=project_id,
    )  # fmt: skip
    return _flow_saved(rt, org_id, project_id)


@router.post("/o/{org_id}/projects/{project_id}/onboarding/steps/{step_id}/delete")
def flow_delete_step(
    org_id: str, project_id: str, step_id: str, rt: RT, session: User, form: UserForm
):
    onboarding.delete_step(
        rt.db, session.user_id, org_id, step_id,
        expected_version=to_int(form.get("version")), project_id=project_id,
    )  # fmt: skip
    return _flow_saved(rt, org_id, project_id)


@router.post("/o/{org_id}/projects/{project_id}/onboarding/volunteers/{user_id}/steps/{step_id}")
def flow_confirm_step(
    org_id: str, project_id: str, user_id: str, step_id: str, rt: RT, session: User,
    _form: UserForm,
):  # fmt: skip
    """The lead side ticks off a volunteer's current step."""
    onboarding.complete_step(
        rt.db, session.user_id, org_id, step_id, user_id=user_id, project_id=project_id
    )
    drain(rt)
    return redirect(f"/o/{org_id}/projects/{project_id}", "step_done")
