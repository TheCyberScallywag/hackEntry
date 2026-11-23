"""Organisations' onboarding workflows over the JSON API, under /api/v1.

Two audiences. A volunteer reads their own runs (`GET /me/onboarding`) and ticks off the steps
that are theirs to complete. An organisation's admins (and system admins) edit the workflow: the
switch, the ordered steps and the email templates, and they see where each volunteer is and
which emails went out. Volunteers' addresses are never part of a response.

Every edit carries the workflow's `version` and answers with the whole workflow as it now is,
new version included, so a client always holds what it needs for its next edit. A stale version
is a 409.

A project of the organisation can have a workflow of its own, for volunteers joining its team,
under /orgs/{org_id}/projects/{project_id}/onboarding. It has the same shape and the same
rules, with two differences that keep it inside the organisation's: it is run by the project's
lead (whom the org admins assign) and the org admins, and it chooses its emails from the
organisation's templates, which only org admins write.

The workflow and each volunteer's place in it are also projected to the Relate graph, which is
where the assistants read them, so every write here is followed by a projection.
"""

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.mailer import BODY_MAX, SUBJECT_MAX
from app.services import onboarding
from app.web.api import Api
from app.web.deps import drain

router = APIRouter(prefix="/api/v1", tags=["onboarding"])

CompletedBy = Literal["volunteer", "org_admin", "lead"]
RunState = Literal["in_progress", "completed", "cancelled"]


# ---------------------------------------------------------------- request bodies
class WorkflowIn(BaseModel):
    enabled: bool
    completion_template_id: str | None = Field(default=None, max_length=64)
    version: int


class StepIn(BaseModel):
    title: str = Field(max_length=200)
    description: str = Field(default="", max_length=2000)
    completed_by: CompletedBy
    email_template_id: str | None = Field(default=None, max_length=64)
    version: int


class MoveIn(BaseModel):
    direction: Literal["up", "down"]
    version: int


class TemplatePreviewIn(BaseModel):
    subject: str = Field(max_length=SUBJECT_MAX * 2)
    body: str = Field(max_length=BODY_MAX * 2)


class TemplateIn(TemplatePreviewIn):
    name: str = Field(max_length=120)
    version: int


# ---------------------------------------------------------------- responses
class WorkflowStep(BaseModel):
    id: str
    position: int
    title: str
    description: str
    completed_by: CompletedBy
    email_template_id: str | None


class EmailTemplate(BaseModel):
    id: str
    name: str
    subject: str
    body: str
    updated_at: str


class Placeholder(BaseModel):
    name: str
    meaning: str


class Completer(BaseModel):
    value: CompletedBy
    label: str


class Workflow(BaseModel):
    enabled: bool
    completion_template_id: str | None
    version: int
    updated_at: str | None
    steps: list[WorkflowStep]
    completers: list[Completer]  # who a step of this workflow may be completed by
    templates: list[EmailTemplate]
    placeholders: list[Placeholder]


class EmailPreview(BaseModel):
    subject: str
    body: str


class Queued(BaseModel):
    queued: bool


class RunStep(BaseModel):
    id: str
    position: int
    title: str
    description: str
    completed_by: CompletedBy
    state: Literal["waiting", "open", "done"]
    done_at: str | None
    can_complete: bool


class OnboardingRun(BaseModel):
    org_id: str
    organisation: str
    project_id: str  # '' for the organisation's own workflow
    project: str  # the project's title, or ''
    state: RunState
    state_label: str
    awaiting_acceptance: bool  # a project's steps are done; its lead side has yet to accept me
    started_at: str
    completed_at: str | None
    steps_done: int
    steps_total: int
    steps: list[RunStep]


class OnboardingVolunteer(BaseModel):
    user_id: str
    display_name: str
    state: RunState
    state_label: str
    started_at: str
    completed_at: str | None
    steps_done: int
    steps_total: int
    current_step_id: str | None
    current_step: str | None
    waiting_for: CompletedBy | None
    awaiting_acceptance: bool  # projects: steps done, not yet accepted onto the team


class SentEmail(BaseModel):
    id: int
    recipient: str
    purpose: Literal["step", "completed", "test"]
    template_name: str
    subject: str
    status: Literal["queued", "sent", "failed", "not_configured"]
    attempts: int
    created_at: str
    sent_at: str | None


def _rt(request: Request):
    return request.app.state.rt


# ---------------------------------------------------------------- the volunteer
@router.get("/me/onboarding")
def my_onboarding(request: Request, session: Api) -> list[OnboardingRun]:
    """My onboarding with each organisation I joined: its steps and where I am."""
    return onboarding.my_onboarding(_rt(request).db, session.user_id)


@router.post("/orgs/{org_id}/onboarding/steps/{step_id}/complete")
def complete_my_step(org_id: str, step_id: str, request: Request, session: Api) -> OnboardingRun:
    """Tick off my current step, if it is one the volunteer completes."""
    rt = _rt(request)
    run = onboarding.complete_step(rt.db, session.user_id, org_id, step_id)
    drain(rt)
    return run


# ---------------------------------------------------------------- org admins: the workflow
def _workflow(request: Request, session, org_id: str) -> dict:
    return onboarding.get_workflow(_rt(request).db, session.user_id, org_id)


def _edited(request: Request, session, org_id: str) -> dict:
    """After an edit: bring the graph's copy of the workflow up to date, then answer with it."""
    drain(_rt(request))
    return _workflow(request, session, org_id)


@router.get("/orgs/{org_id}/onboarding")
def get_workflow(org_id: str, request: Request, session: Api) -> Workflow:
    """The organisation's workflow: switch, steps, email templates and the placeholders a
    template may use (org admins)."""
    return _workflow(request, session, org_id)


@router.put("/orgs/{org_id}/onboarding")
def update_workflow(org_id: str, body: WorkflowIn, request: Request, session: Api) -> Workflow:
    """Switch the workflow on or off and choose the email sent on completion. Switching on
    applies to volunteers who join from then on."""
    onboarding.update_workflow(
        _rt(request).db, session.user_id, org_id, enabled=body.enabled,
        completion_template_id=body.completion_template_id, expected_version=body.version,
    )  # fmt: skip
    return _edited(request, session, org_id)


@router.post("/orgs/{org_id}/onboarding/steps", status_code=201)
def add_step(org_id: str, body: StepIn, request: Request, session: Api) -> Workflow:
    onboarding.add_step(
        _rt(request).db, session.user_id, org_id, title=body.title,
        description=body.description, completed_by=body.completed_by,
        email_template_id=body.email_template_id, expected_version=body.version,
    )  # fmt: skip
    return _edited(request, session, org_id)


@router.put("/orgs/{org_id}/onboarding/steps/{step_id}")
def update_step(
    org_id: str, step_id: str, body: StepIn, request: Request, session: Api
) -> Workflow:
    onboarding.update_step(
        _rt(request).db, session.user_id, org_id, step_id, title=body.title,
        description=body.description, completed_by=body.completed_by,
        email_template_id=body.email_template_id, expected_version=body.version,
    )  # fmt: skip
    return _edited(request, session, org_id)


@router.post("/orgs/{org_id}/onboarding/steps/{step_id}/move")
def move_step(org_id: str, step_id: str, body: MoveIn, request: Request, session: Api) -> Workflow:
    onboarding.move_step(
        _rt(request).db, session.user_id, org_id, step_id, direction=body.direction,
        expected_version=body.version,
    )  # fmt: skip
    return _edited(request, session, org_id)


@router.delete("/orgs/{org_id}/onboarding/steps/{step_id}")
def delete_step(
    org_id: str, step_id: str, version: int, request: Request, session: Api
) -> Workflow:
    """Remove a step. Volunteers waiting on it move to the next one."""
    onboarding.delete_step(
        _rt(request).db, session.user_id, org_id, step_id, expected_version=version
    )
    return _edited(request, session, org_id)


# ---------------------------------------------------------------- org admins: email templates
@router.post("/orgs/{org_id}/onboarding/templates", status_code=201)
def add_template(org_id: str, body: TemplateIn, request: Request, session: Api) -> Workflow:
    onboarding.save_template(
        _rt(request).db, session.user_id, org_id, name=body.name, subject=body.subject,
        body=body.body, expected_version=body.version,
    )  # fmt: skip
    return _edited(request, session, org_id)


@router.post("/orgs/{org_id}/onboarding/templates/preview")
def preview_template(
    org_id: str, body: TemplatePreviewIn, request: Request, session: Api
) -> EmailPreview:
    """The email filled in with sample values. Saves and sends nothing."""
    rt = _rt(request)
    return onboarding.preview_template(
        rt.db, session.user_id, org_id, subject=body.subject, body=body.body,
        public_url=rt.settings.public_url,
    )  # fmt: skip


@router.put("/orgs/{org_id}/onboarding/templates/{template_id}")
def update_template(
    org_id: str, template_id: str, body: TemplateIn, request: Request, session: Api
) -> Workflow:
    onboarding.save_template(
        _rt(request).db, session.user_id, org_id, template_id, name=body.name,
        subject=body.subject, body=body.body, expected_version=body.version,
    )  # fmt: skip
    return _edited(request, session, org_id)


@router.delete("/orgs/{org_id}/onboarding/templates/{template_id}")
def delete_template(
    org_id: str, template_id: str, version: int, request: Request, session: Api
) -> Workflow:
    """Delete a template. Steps that used it send no email from then on."""
    onboarding.delete_template(
        _rt(request).db, session.user_id, org_id, template_id, expected_version=version
    )
    return _edited(request, session, org_id)


@router.post("/orgs/{org_id}/onboarding/templates/{template_id}/test")
def send_test(org_id: str, template_id: str, request: Request, session: Api) -> Queued:
    """Queue the template, with sample values, to my own address."""
    onboarding.send_test(_rt(request).db, session.user_id, org_id, template_id)
    return {"queued": True}


# ---------------------------------------------------------------- org admins: progress and mail
@router.get("/orgs/{org_id}/onboarding/volunteers")
def onboarding_volunteers(org_id: str, request: Request, session: Api) -> list[OnboardingVolunteer]:
    """Where each volunteer is in the workflow. No contact details."""
    return onboarding.org_progress(_rt(request).db, session.user_id, org_id)


@router.post("/orgs/{org_id}/onboarding/volunteers/{user_id}/steps/{step_id}/complete")
def complete_for_volunteer(
    org_id: str, user_id: str, step_id: str, request: Request, session: Api
) -> OnboardingRun:
    """An org admin ticks off a volunteer's current step."""
    rt = _rt(request)
    run = onboarding.complete_step(rt.db, session.user_id, org_id, step_id, user_id=user_id)
    drain(rt)
    return run


@router.get("/orgs/{org_id}/onboarding/emails")
def onboarding_emails(org_id: str, request: Request, session: Api) -> list[SentEmail]:
    """The organisation's recent emails and whether each was sent. Names, never addresses."""
    return onboarding.sent_emails(_rt(request).db, session.user_id, org_id)


# ---------------------------------------------------------------- a project's own workflow
PROJECT = "/orgs/{org_id}/projects/{project_id}/onboarding"


def _project_flow(request: Request, session, org_id: str, project_id: str) -> dict:
    return onboarding.get_workflow(_rt(request).db, session.user_id, org_id, project_id)


def _project_edited(request: Request, session, org_id: str, project_id: str) -> dict:
    drain(_rt(request))
    return _project_flow(request, session, org_id, project_id)


@router.get(PROJECT)
def get_project_workflow(org_id: str, project_id: str, request: Request, session: Api) -> Workflow:
    """The project's own onboarding workflow, for volunteers joining its team (the project's
    lead and org admins). `templates` are the organisation's, to choose from."""
    return _project_flow(request, session, org_id, project_id)


@router.put(PROJECT)
def update_project_workflow(
    org_id: str, project_id: str, body: WorkflowIn, request: Request, session: Api
) -> Workflow:
    """Switch the project's workflow on or off. Switched on, a volunteer who accepts an
    invitation goes through the steps, and the lead side then accepts them onto the team."""
    onboarding.update_workflow(
        _rt(request).db, session.user_id, org_id, enabled=body.enabled,
        completion_template_id=body.completion_template_id, expected_version=body.version,
        project_id=project_id,
    )  # fmt: skip
    return _project_edited(request, session, org_id, project_id)


@router.post(PROJECT + "/steps", status_code=201)
def add_project_step(
    org_id: str, project_id: str, body: StepIn, request: Request, session: Api
) -> Workflow:
    onboarding.add_step(
        _rt(request).db, session.user_id, org_id, title=body.title,
        description=body.description, completed_by=body.completed_by,
        email_template_id=body.email_template_id, expected_version=body.version,
        project_id=project_id,
    )  # fmt: skip
    return _project_edited(request, session, org_id, project_id)


@router.put(PROJECT + "/steps/{step_id}")
def update_project_step(
    org_id: str, project_id: str, step_id: str, body: StepIn, request: Request, session: Api
) -> Workflow:
    onboarding.update_step(
        _rt(request).db, session.user_id, org_id, step_id, title=body.title,
        description=body.description, completed_by=body.completed_by,
        email_template_id=body.email_template_id, expected_version=body.version,
        project_id=project_id,
    )  # fmt: skip
    return _project_edited(request, session, org_id, project_id)


@router.post(PROJECT + "/steps/{step_id}/move")
def move_project_step(
    org_id: str, project_id: str, step_id: str, body: MoveIn, request: Request, session: Api
) -> Workflow:
    onboarding.move_step(
        _rt(request).db, session.user_id, org_id, step_id, direction=body.direction,
        expected_version=body.version, project_id=project_id,
    )  # fmt: skip
    return _project_edited(request, session, org_id, project_id)


@router.delete(PROJECT + "/steps/{step_id}")
def delete_project_step(
    org_id: str, project_id: str, step_id: str, version: int, request: Request, session: Api
) -> Workflow:
    onboarding.delete_step(
        _rt(request).db, session.user_id, org_id, step_id, expected_version=version,
        project_id=project_id,
    )  # fmt: skip
    return _project_edited(request, session, org_id, project_id)


@router.post(PROJECT + "/steps/{step_id}/complete")
def complete_my_project_step(
    org_id: str, project_id: str, step_id: str, request: Request, session: Api
) -> OnboardingRun:
    """Tick off my current step of the project's onboarding, if it is one the volunteer
    completes."""
    rt = _rt(request)
    run = onboarding.complete_step(rt.db, session.user_id, org_id, step_id, project_id=project_id)
    drain(rt)
    return run


@router.get(PROJECT + "/volunteers")
def project_onboarding_volunteers(
    org_id: str, project_id: str, request: Request, session: Api
) -> list[OnboardingVolunteer]:
    """Where each volunteer is in the project's workflow (the lead side). No contact details."""
    return onboarding.org_progress(_rt(request).db, session.user_id, org_id, project_id)


@router.post(PROJECT + "/volunteers/{user_id}/steps/{step_id}/complete")
def complete_project_step_for_volunteer(
    org_id: str, project_id: str, user_id: str, step_id: str, request: Request, session: Api
) -> OnboardingRun:
    """The project's lead or an org admin ticks off a volunteer's current step."""
    rt = _rt(request)
    run = onboarding.complete_step(
        rt.db, session.user_id, org_id, step_id, user_id=user_id, project_id=project_id
    )
    drain(rt)
    return run
