"""Project onboarding assistants: one for the person suggesting a project, one for its reviewer.

**Scoping** (`project-scoping`) is for an active volunteer who belongs to the organisation. It
turns a rough idea into a suggestion a reviewer can approve: a title, a summary, one cause, the
skills the project needs. It can save that as a private draft once the author approves. It never
submits a suggestion for review: that stays the author's own step.

**Review** (`project-review`) is for the organisation's reviewers and admins, on a suggestion
that is waiting for them. It checks the suggestion against the organisation's other projects
and the catalogue, and recommends a decision with a reason written for the author. It has no
tool that changes anything: the reviewer decides, and their name goes on the decision.

Both are checked by the application's own rules: a proposal must pass the same validation as
the suggestion form (`services.Draft.validate`), and the review reads the suggestion through
`services.get_submission`, so it sees exactly what that reviewer may see.
"""

from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent, ApprovalRequired, DeferredToolRequests, ModelRetry

from app import matching, services
from app.agents.deps import (
    HOUSE_RULES,
    MODEL_SETTINGS,
    RETRIES,
    AgentDeps,
    Ctx,
    catalogue_tools,
    guard,
)
from app.agents.evaluators import online
from app.agents.runner import Assistant, register
from app.catalogue import get_catalogue


# ---------------------------------------------------------------- shared reads
class ActiveProject(BaseModel):
    project_id: str
    title: str
    summary: str
    cause: str
    required_skills: list[str]


class OrganisationBrief(BaseModel):
    name: str
    description: str
    active_projects: list[ActiveProject]


def organisation_brief(ctx: Ctx) -> OrganisationBrief:
    """The organisation this is for: what it does and the projects it already runs. Check new
    work against these so a suggestion does not duplicate one."""
    deps = ctx.deps
    with deps.db.read() as conn:
        org = services.get_org(conn, deps.org_id or "")
    rows = matching.browse(deps.db, deps.graph, deps.actor_id, org["id"])
    return OrganisationBrief(
        name=org["name"],
        description=org["description"],
        active_projects=[
            ActiveProject(
                project_id=r["project"],
                title=r["title"],
                summary=r["summary"],
                cause=r["cause_name"],
                required_skills=r["required_names"],
            )  # fmt: skip
            for r in rows
        ],
    )


# ---------------------------------------------------------------- scoping (the author)
class ProjectProposal(BaseModel):
    """A project suggestion, complete enough to send for review."""

    title: str = Field(min_length=3, max_length=services.TITLE_MAX)
    summary: str = Field(
        min_length=10, max_length=services.SUMMARY_MAX,
        description="What will be done, for whom, and what done looks like",
    )  # fmt: skip
    cause_id: str
    required_skills: list[str] = Field(min_length=1, description="Skill IDs the work needs")
    optional_skills: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=8, description="Why these skills and this cause")
    open_questions: list[str] = Field(
        default_factory=list, description="What a reviewer is likely to ask before approving"
    )
    overlaps_with: list[str] = Field(
        default_factory=list, description="IDs of active projects this overlaps with, if any"
    )


scoping_agent: Agent[AgentDeps, ProjectProposal | DeferredToolRequests] = Agent(
    None,
    name="project_scoping",
    deps_type=AgentDeps,
    output_type=[ProjectProposal, DeferredToolRequests],
    instructions=(
        "You help a volunteer shape a project idea into a suggestion their organisation's "
        "reviewers can approve.\n\n"
        "How to work:\n"
        "1. Read the organisation (organisation_brief), the author's earlier suggestions "
        "(my_suggestions) and the catalogue. find_skills looks a skill up by the words the "
        "author used; a related skill is close to another, never a substitute for it.\n"
        "2. Write a specific title and a summary that says what will be done, for whom, and "
        "what finished looks like. Use only what the author told you; put anything you had to "
        "assume in open_questions rather than in the summary.\n"
        "3. Choose exactly one cause. Require only the skills the work cannot be done without; "
        "put nice-to-haves in optional_skills. Fewer required skills means more volunteers "
        "can match.\n"
        "4. If it overlaps an active project, say which in overlaps_with.\n"
        "5. Call save_draft with the suggestion. It waits for the author's approval and saves a "
        "private draft; it does not send anything for review.\n\n" + HOUSE_RULES
    ),
    toolsets=[catalogue_tools],
    tools=[organisation_brief],
    capabilities=[guard, online("project_scoping")],
    model_settings=MODEL_SETTINGS,
    retries=RETRIES,
    defer_model_check=True,
)


class OwnSuggestion(BaseModel):
    title: str
    organisation: str
    state: str


@scoping_agent.tool
def my_suggestions(ctx: Ctx) -> list[OwnSuggestion]:
    """The author's own earlier suggestions and where each stands."""
    return [
        OwnSuggestion(
            title=s["title"],
            organisation=s["org_name"],
            state=services.PROJECT_STATES[s["state"]],
        )  # fmt: skip
        for s in services.my_submissions(ctx.deps.db, ctx.deps.actor_id)
    ]


@scoping_agent.tool
def save_draft(
    ctx: Ctx,
    title: str,
    summary: str,
    cause_id: str,
    required_skills: list[str],
    optional_skills: list[str],
) -> str:
    """Save the suggestion as the author's private draft, after they approve. Nobody else sees
    a draft and nobody is notified; the author submits it for review themselves.

    Args:
        title: 3 to 120 characters.
        summary: What will be done, for whom, and what done looks like.
        cause_id: One cause ID from list_causes.
        required_skills: Skill IDs the work cannot be done without.
        optional_skills: Skill IDs that would help.
    """
    deps = ctx.deps
    # The same validation as the suggestion form; Invalid goes back to the model (deps.guard).
    draft = services.Draft(title, summary, cause_id, required_skills, optional_skills).validate()
    if not ctx.tool_call_approved:
        raise ApprovalRequired(
            metadata={"summary": f"Save “{draft.title}” as a private draft suggestion."}
        )
    if deps.subject_id:  # an existing draft of theirs: saved again in place
        current = services.get_submission(deps.db, deps.actor_id, deps.subject_id)
        services.update_draft(
            deps.db, deps.actor_id, deps.subject_id, deps.org_id or "", draft,
            expected_version=current["version"],
        )  # fmt: skip
        return "Draft updated. It is still private; submit it for review when you are ready."
    sid = services.save_draft(deps.db, deps.actor_id, deps.org_id or "", draft)
    deps.subject_id = sid
    return f"Draft saved (id {sid}). It is private; submit it for review when you are ready."


@scoping_agent.output_validator
def _valid_proposal(ctx: Ctx, proposal: ProjectProposal | DeferredToolRequests):
    if not isinstance(proposal, ProjectProposal):
        return proposal
    try:
        draft = services.Draft(
            proposal.title, proposal.summary, proposal.cause_id,
            proposal.required_skills, proposal.optional_skills,
        ).validate()  # fmt: skip
    except services.Invalid as error:
        raise ModelRetry(str(error)) from error
    active = {r["project"] for r in matching.browse(
        ctx.deps.db, ctx.deps.graph, ctx.deps.actor_id, ctx.deps.org_id or ""
    )}  # fmt: skip
    if unknown := sorted(set(proposal.overlaps_with) - active):
        raise ModelRetry(f"overlaps_with has unknown project IDs: {unknown}.")
    # Normalised the way the form would store it (sorted, required wins over optional).
    proposal.title, proposal.summary = draft.title, draft.summary
    proposal.required_skills, proposal.optional_skills = draft.required, draft.optional
    return proposal


def _may_scope(deps: AgentDeps) -> None:
    services.require_active(deps.db, deps.actor_id)
    services.require_access(deps.db, deps.actor_id, deps.org_id or "")
    if deps.subject_id:
        sub = services.get_submission(deps.db, deps.actor_id, deps.subject_id)
        if not sub["can_edit"]:
            raise services.Conflict("Only your own draft can be reworked with the assistant.")


scoping = register(
    Assistant(
        name="project-scoping",
        title="Project scoping assistant",
        audience="Active volunteers, for an organisation they belong to",
        description=(
            "Describe a project idea. It drafts the title, summary, cause and skills a reviewer "
            "needs, flags overlaps with active projects, and saves a private draft once you "
            "approve."
        ),
        agent=scoping_agent,
        authorise=_may_scope,
    )
)


# ---------------------------------------------------------------- review (the reviewer)
class ReviewCheck(BaseModel):
    name: str = Field(description="What was checked, e.g. 'Summary says what done looks like'")
    passed: bool
    note: str = Field(min_length=3)


class ReviewAssessment(BaseModel):
    """A recommendation for the reviewer. The reviewer decides."""

    recommendation: Literal["approved", "changes_requested", "rejected"]
    reason_for_author: str = Field(
        min_length=10, max_length=services.REASON_MAX,
        description="Written to the author: specific, kind, and says what to change if anything",
    )  # fmt: skip
    private_note: str = Field(
        default="", max_length=services.NOTE_MAX, description="For fellow reviewers only"
    )
    checks: list[ReviewCheck] = Field(min_length=1)
    overlaps_with: list[str] = Field(default_factory=list, description="Active project IDs")


review_agent: Agent[AgentDeps, ReviewAssessment] = Agent(
    None,
    name="project_review",
    deps_type=AgentDeps,
    output_type=ReviewAssessment,
    instructions=(
        "You help an organisation's reviewer assess a project suggestion. You recommend; the "
        "reviewer decides and is accountable for the decision.\n\n"
        "Read the suggestion (suggestion), the organisation and its active projects "
        "(organisation_brief) and the catalogue. Then check, and report each as a check:\n"
        "- the summary says what will be done, for whom, and what finished looks like;\n"
        "- the cause fits the work and the organisation;\n"
        "- every required skill is really needed, and nothing obviously needed is missing;\n"
        "- it does not duplicate an active project;\n"
        "- earlier review feedback, if any, has been addressed.\n\n"
        "Recommend `approved` only if every check passes. Recommend `changes_requested` when "
        "the idea is sound but the suggestion is not yet clear enough to run. Recommend "
        "`rejected` only when it does not fit the organisation at all or duplicates an active "
        "project. reason_for_author is read by a volunteer: be specific and kind.\n\n" + HOUSE_RULES
    ),
    toolsets=[catalogue_tools],
    tools=[organisation_brief],
    capabilities=[guard, online("project_review")],
    model_settings=MODEL_SETTINGS,
    retries=RETRIES,
    defer_model_check=True,
)


class EarlierReview(BaseModel):
    revision: int
    action: str
    reason_for_author: str


class SuggestionInfo(BaseModel):
    title: str
    summary: str
    cause_id: str
    cause: str
    required_skills: list[str]  # IDs
    optional_skills: list[str]  # IDs
    revision: int
    author: str
    earlier_reviews: list[EarlierReview]


@review_agent.tool
def suggestion(ctx: Ctx) -> SuggestionInfo:
    """The suggestion under review, at its current revision, with earlier review feedback."""
    sub = services.get_submission(ctx.deps.db, ctx.deps.actor_id, ctx.deps.subject_id)
    current = sub["current"]
    return SuggestionInfo(
        title=current["title"],
        summary=current["summary"],
        cause_id=current["cause_id"] or "",
        cause=get_catalogue().cause_name(current["cause_id"] or ""),
        required_skills=current["required"],
        optional_skills=current["optional"],
        revision=current["revision"],
        author=sub["author_name"],
        earlier_reviews=[
            EarlierReview(
                revision=r["revision"],
                action=r["action"],
                reason_for_author=r["reason_for_author"],
            )  # fmt: skip
            for r in sub["reviews"]
        ],
    )


@review_agent.output_validator
def _consistent(ctx: Ctx, assessment: ReviewAssessment) -> ReviewAssessment:
    failed = [c.name for c in assessment.checks if not c.passed]
    if assessment.recommendation == "approved" and failed:
        raise ModelRetry(
            f"You recommend approval but these checks failed: {failed}. Recommend "
            "changes_requested, or explain in the check why it passes."
        )
    active = {r["project"] for r in matching.browse(
        ctx.deps.db, ctx.deps.graph, ctx.deps.actor_id, ctx.deps.org_id or ""
    )}  # fmt: skip
    if unknown := sorted(set(assessment.overlaps_with) - active):
        raise ModelRetry(f"overlaps_with has unknown project IDs: {unknown}.")
    return assessment


def _may_review(deps: AgentDeps) -> None:
    services.require_access(deps.db, deps.actor_id, deps.org_id or "", role="reviewer")
    sub = services.get_submission(deps.db, deps.actor_id, deps.subject_id)
    if sub["org_id"] != deps.org_id:
        raise services.NotFound("submission")
    if not sub["can_review"]:
        raise services.Conflict(
            "This suggestion is not waiting for your review (it is your own, or already decided)."
        )


review = register(
    Assistant(
        name="project-review",
        title="Review assistant",
        audience="Reviewers and admins of the organisation, for a suggestion awaiting review",
        description=(
            "Checks a suggestion for clarity, fit, skills and overlap, and recommends a decision "
            "with a reason for the author. You make the decision."
        ),
        agent=review_agent,
        authorise=_may_review,
    )
)
