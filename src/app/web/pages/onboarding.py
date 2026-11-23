"""A volunteer's onboarding with the organisations they joined, and with the projects whose
teams they are joining: each one's own steps."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.services import onboarding
from app.web.deps import RT, User, UserForm, drain, redirect, render

router = APIRouter(include_in_schema=False, default_response_class=HTMLResponse)


@router.get("/onboarding")
def onboarding_page(request: Request, rt: RT, session: User):
    runs = onboarding.my_onboarding(rt.db, session.user_id)
    return render(request, "onboarding.html", session, runs=runs)


@router.post("/onboarding/{org_id}/steps/{step_id}/complete")
def complete_step(org_id: str, step_id: str, rt: RT, session: User, form: UserForm):
    onboarding.complete_step(
        rt.db, session.user_id, org_id, step_id, project_id=str(form.get("project_id", ""))
    )
    drain(rt)
    return redirect("/onboarding", "step_done")
