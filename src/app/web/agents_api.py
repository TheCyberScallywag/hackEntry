"""The assistants and the audit trail over the JSON API, under /api/v1.

Each assistant has its own route, so its inputs are typed and its access rule is plain: the route
names the organisation, project or suggestion, and app.agents.runner checks the caller against
it before a model is asked anything. A run answers in one of two ways: `completed`, with the
assistant's output, or `awaiting_approval`, with the actions it wants to take. Nothing is
changed until the caller approves them (`POST /agents/runs/{run_id}/approval`).

With no model configured (AGENT_MODEL) the run routes answer 503; the listing and the audit
routes still work. 429 means the account's daily quota is used up, 502 that the model failed or
a limit stopped the run.
"""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from app import agents, audit, services
from app.web.api import Api

router = APIRouter(prefix="/api/v1")

Prompt = Annotated[str, Field(min_length=1, max_length=4000)]
Days = Annotated[int, Field(ge=1, le=365)]


# ---------------------------------------------------------------- schemas
class AssistantInfo(BaseModel):
    name: str
    title: str
    audience: str
    description: str
    enabled: bool


class PendingAction(BaseModel):
    tool_call_id: str
    tool: str
    args: dict[str, Any]
    summary: str = ""


class RunUsage(BaseModel):
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0


class AgentRun(BaseModel):
    """`output` is the assistant's own result type; see app.agents for each one."""

    run_id: str
    agent: str
    status: Literal["completed", "awaiting_approval"]
    output: dict[str, Any] | None = None
    pending: list[PendingAction] = []
    usage: RunUsage = RunUsage()


class RunRecord(BaseModel):
    id: str
    agent: str
    org_id: str | None
    subject_id: str
    status: str
    model: str
    requests: int
    input_tokens: int
    output_tokens: int
    tool_calls: int
    tools: list[str]
    error: str
    output: dict[str, Any] | None
    pending: list[PendingAction]
    trace_id: str
    created_at: str
    finished_at: str | None


class OnboardingIn(BaseModel):
    message: Prompt = Field(description="What the volunteer can do and cares about")


class ScopingIn(BaseModel):
    org_id: str
    idea: Prompt
    submission_id: str = Field("", description="An existing draft of mine to rework")


class ReviewIn(BaseModel):
    org_id: str
    submission_id: str
    question: str = Field("", max_length=1000)


class ShortlistIn(BaseModel):
    project_id: str
    question: str = Field("", max_length=1000, description="e.g. 'invite the top two'")


class ExplainIn(BaseModel):
    org_id: str
    question: str = Field("", max_length=1000)


class AuditIn(BaseModel):
    days: Days = 7
    question: str = Field("", max_length=1000)


class ApprovalIn(BaseModel):
    approve: bool
    reason: str = Field("", max_length=services.REASON_MAX)


class AuditEventOut(BaseModel):
    id: int
    created_at: str
    actor_id: str | None
    actor: str | None
    action: str
    outcome: str
    target_kind: str
    target_id: str
    org_id: str | None
    org: str | None
    source: str
    detail: dict[str, Any]


class RoleHolder(BaseModel):
    user_id: str
    display_name: str
    status: str
    role: str
    granted_at: str
    granted_by: str | None = None


class OrgRoles(BaseModel):
    org_id: str
    name: str
    volunteers: int
    roles: list[RoleHolder]


class RbacSnapshot(BaseModel):
    system_roles: list[RoleHolder]
    organisations: list[OrgRoles]


class ActionCount(BaseModel):
    action: str
    outcome: str
    events: int


class Refused(BaseModel):
    actor_id: str
    display_name: str
    refusals: int


class AgentUsage(BaseModel):
    agent: str
    runs: int
    people: int
    completed: int
    failed: int
    awaiting_approval: int
    requests: int
    input_tokens: int
    output_tokens: int
    tool_calls: int


class UsageSummary(BaseModel):
    days: int
    since: str
    outcomes: dict[str, int]
    actions: list[ActionCount]
    most_refused: list[Refused]
    agents: list[AgentUsage]


class CheckOut(BaseModel):
    id: str
    severity: Literal["high", "medium", "low"]
    title: str
    evidence: str
    subjects: list[str]


def _rt(request: Request):
    return request.app.state.rt


def _start(request: Request, session, name: str, prompt: str, **kw) -> agents.Run:
    return agents.start(_rt(request), name, actor_id=session.user_id, prompt=prompt, **kw)


# ---------------------------------------------------------------- assistants
@router.get("/agents", tags=["assistants"])
def list_assistants(request: Request, session: Api) -> list[AssistantInfo]:
    """The assistants and who each is for. `enabled` is false when no model is configured."""
    enabled = _rt(request).agent_model is not None
    return [
        AssistantInfo(
            name=a.name,
            title=a.title,
            audience=a.audience,
            description=a.description,
            enabled=enabled,
        )  # fmt: skip
        for a in sorted(agents.REGISTRY.values(), key=lambda a: a.name)
    ]


@router.post("/agents/volunteer-onboarding", tags=["assistants"])
def onboarding(body: OnboardingIn, request: Request, session: Api) -> AgentRun:
    """Propose my profile from what I say I can do. Saving it, and sending it for vetting, each
    wait for my approval."""
    return _start(request, session, "volunteer-onboarding", body.message)


@router.post("/agents/project-scoping", tags=["assistants"])
def scoping(body: ScopingIn, request: Request, session: Api) -> AgentRun:
    """Shape a project idea into a suggestion (active members of the organisation). Saving the
    draft waits for my approval; submitting it for review stays my own step."""
    return _start(request, session, "project-scoping", body.idea, org_id=body.org_id,
                  subject_id=body.submission_id)  # fmt: skip


@router.post("/agents/project-review", tags=["assistants"])
def review(body: ReviewIn, request: Request, session: Api) -> AgentRun:
    """A recommendation on a suggestion awaiting my review (reviewers and org admins). It
    changes nothing: I record the decision myself."""
    return _start(request, session, "project-review",
                  body.question or "Assess this suggestion.", org_id=body.org_id,
                  subject_id=body.submission_id)  # fmt: skip


@router.post("/agents/match-shortlist", tags=["assistants"])
def shortlist(body: ShortlistIn, request: Request, session: Api) -> AgentRun:
    """Who to invite to a project's team, from the graph's candidates (the lead or an org
    admin). An invitation waits for my approval."""
    return _start(request, session, "match-shortlist",
                  body.question or "Who should I invite?", subject_id=body.project_id)  # fmt: skip


@router.post("/agents/match-explainer", tags=["assistants"])
def explain(body: ExplainIn, request: Request, session: Api) -> AgentRun:
    """Why I match this organisation's projects, and what my own exclusions rule out."""
    return _start(request, session, "match-explainer",
                  body.question or "Explain my matches.", org_id=body.org_id)  # fmt: skip


@router.post("/agents/audit", tags=["assistants"])
def audit_report(body: AuditIn, request: Request, session: Api) -> AgentRun:
    """An access and usage audit for the last `days` days (system admins)."""
    return _start(request, session, "audit", body.question, days=body.days)


@router.post("/agents/runs/{run_id}/approval", tags=["assistants"])
def approve(run_id: str, body: ApprovalIn, request: Request, session: Api) -> AgentRun:
    """Approve or refuse what a waiting run wants to do. Only whoever started it may answer."""
    return agents.resume(_rt(request), actor_id=session.user_id, run_id=run_id,
                         approve=body.approve, reason=body.reason)  # fmt: skip


@router.get("/agents/runs", tags=["assistants"])
def my_runs(
    request: Request, session: Api, limit: Annotated[int, Query(ge=1, le=100)] = 20
) -> list[RunRecord]:
    """My own assistant runs, newest first."""
    return agents.my_runs(_rt(request), session.user_id, limit=limit)


@router.get("/agents/runs/{run_id}", tags=["assistants"])
def get_run(run_id: str, request: Request, session: Api) -> RunRecord:
    return agents.get_run(_rt(request), session.user_id, run_id)


# ---------------------------------------------------------------- audit (system admins)
@router.get("/admin/audit/events", tags=["audit"])
def audit_events(
    request: Request,
    session: Api,
    action: str = "",
    outcome: str = "",
    user_id: str = "",
    limit: Annotated[int, Query(ge=1, le=audit.MAX_EVENTS)] = 100,
) -> list[AuditEventOut]:
    """The audit trail, newest first. `action` is a prefix: `role.`, `http.`, `agent.`, `auth.`."""
    return audit.list_events(_rt(request).db, session.user_id, action=action, outcome=outcome,
                             user_id=user_id, limit=limit)  # fmt: skip


@router.get("/admin/audit/rbac", tags=["audit"])
def audit_rbac(request: Request, session: Api) -> RbacSnapshot:
    """Who holds which role now, system-wide and per organisation."""
    return audit.rbac_snapshot(_rt(request).db, session.user_id)


@router.get("/admin/audit/usage", tags=["audit"])
def audit_usage(request: Request, session: Api, days: Days = 7) -> UsageSummary:
    """Events by action and outcome, the accounts refused most, and what each assistant cost."""
    return audit.usage_summary(_rt(request).db, session.user_id, days=days)


@router.get("/admin/audit/checks", tags=["audit"])
def audit_checks(request: Request, session: Api, days: Days = 7) -> list[CheckOut]:
    """The automatic findings: role and usage problems the application can prove from its data."""
    return [c.as_dict() for c in audit.checks(_rt(request).db, session.user_id, days=days)]
