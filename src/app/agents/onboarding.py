"""Volunteer onboarding assistant: turns "here is what I can do" into a profile.

For the volunteer themselves, about their own account only. It reads the shared catalogue and
their current profile, proposes skills and cause preferences with a reason for each, and can
save them and send the profile for vetting, each only after the volunteer approves.

It also follows each organisation's own onboarding workflow (app.services.onboarding). The
workflow is held in the Relate graph, and the assistant reads it there: which steps an
organisation asks of a new volunteer, which step this volunteer has reached, and who completes
it. It can tick off the volunteer's current step when it is theirs to complete, after approval.
A project's own workflow, for a volunteer joining its team, is read from the graph and followed
in exactly the same way; there the other party is the project's lead (or an org admin), who
confirms their steps and, once every step is done, accepts the volunteer onto the team.

What it must never do, and how that is enforced rather than hoped for:

* invent a skill, cause or organisation: the output validator checks every ID;
* touch an exclusion: `save_profile` and the validator both refuse a change to a skill or cause
  the volunteer has excluded. Exclusions are theirs to lift, on the profile page;
* join an organisation for them: joining makes a volunteer visible to that organisation, so it
  is only ever suggested;
* say a profile is ready for vetting when it offers nothing: the validator checks the facts;
* skip ahead in an organisation's or a project's workflow, or tick off a step that an
  organisation admin or the project lead has to confirm: `complete_onboarding_step` checks the
  graph first and the service checks again, and the validator refuses advice about a step the
  volunteer has not reached;
* put the volunteer on a team: finishing a project's steps only makes them ready, and accepting
  them is the lead side's decision. The assistant has no tool for it.
"""

from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent, ApprovalRequired, DeferredToolRequests, ModelRetry

from app import services
from app.agents.deps import (
    HOUSE_RULES,
    MODEL_SETTINGS,
    RETRIES,
    AgentDeps,
    Ctx,
    catalogue_tools,
    guard,
    self_tools,
)
from app.agents.evaluators import online
from app.agents.runner import Assistant, register
from app.catalogue import get_catalogue
from app.graph import cypher

SkillState = Literal["has", "offers", "exclude"]
CauseStance = Literal["prefer", "exclude"]


class SkillSuggestion(BaseModel):
    skill_id: str
    state: SkillState = Field(description="has: have it; offers: have it and offer it")
    reason: str = Field(min_length=8, description="What in their message supports this")


class CauseSuggestion(BaseModel):
    cause_id: str
    stance: CauseStance
    reason: str = Field(min_length=8)


class OrganisationSuggestion(BaseModel):
    org_id: str
    reason: str = Field(min_length=8)


class StepAdvice(BaseModel):
    """What to do about the step the volunteer has reached in one organisation's workflow, or
    in the workflow of one of its projects."""

    org_id: str
    step_id: str
    advice: str = Field(min_length=8, description="What they do now, or who they wait for")


class OnboardingAdvice(BaseModel):
    """Where the volunteer's onboarding stands and what to do next."""

    summary: str = Field(min_length=10, description="Two or three sentences, to the volunteer")
    skills: list[SkillSuggestion] = Field(default_factory=list)
    causes: list[CauseSuggestion] = Field(default_factory=list)
    organisations: list[OrganisationSuggestion] = Field(
        default_factory=list, description="Organisations they might join; they join themselves"
    )
    missing: list[str] = Field(
        default_factory=list, description="What is still needed before vetting, as questions"
    )
    ready_for_vetting: bool = Field(
        description="True only if the saved profile offers at least one skill"
    )
    next_steps: list[StepAdvice] = Field(
        default_factory=list,
        description="Their current step in each organisation's or project's onboarding "
        "(my_onboarding)",
    )


agent: Agent[AgentDeps, OnboardingAdvice | DeferredToolRequests] = Agent(
    None,
    name="volunteer_onboarding",
    deps_type=AgentDeps,
    output_type=[OnboardingAdvice, DeferredToolRequests],
    instructions=(
        "You help a new volunteer set up their Volunteer Network profile so a moderator can vet "
        "them and projects can be matched to them.\n\n"
        "How to work:\n"
        "1. Read their profile (my_profile) and the catalogue (list_skills, list_causes) first.\n"
        '   find_skills looks a skill up by the words the volunteer used ("excel", '
        '"dashboards"); a skill listed as related is close to another, never the same.\n'
        "2. Map what they tell you to catalogue skills. Use `offers` only for something they "
        "say they want to do for others, `has` for something they can do but did not offer. "
        "Suggest `exclude` only when they say they do not want to be asked for it.\n"
        "3. When their message supports at least one skill, call save_profile with the skills "
        "and causes you propose. It waits for their approval; nothing is saved without it.\n"
        "4. If the saved profile offers a skill and their status allows it, and they asked to be "
        "put forward, call submit_for_vetting. Otherwise tell them it is the next step.\n"
        "5. If they have told you too little, save nothing and ask in `missing`.\n"
        "Suggest organisations (list_organisations) whose description fits; never join for "
        "them. An organisation can have its own onboarding steps for new volunteers: "
        "list_organisations shows them, so say what joining involves.\n"
        "6. Call my_onboarding to see the organisations' own steps they are part-way through. "
        "For each step marked `current`, say in `next_steps` what to do. If that step is "
        "completed by the volunteer and they tell you they have done it, call "
        "complete_onboarding_step. A step completed by `org_admin` is confirmed by the "
        "organisation: tell them to wait, never try to complete it. Steps are taken in order.\n"
        "7. my_onboarding also lists the projects whose teams they are joining (a `project_id` "
        "is set). Those work the same way, inside the organisation's rules: a step completed "
        "by `lead` is confirmed by the project's lead or an org admin, never by the volunteer. "
        "When `awaiting_acceptance` is true they have finished the project's steps and the "
        "project lead decides when they join the team: say so, and do not say they are on the "
        "team until `on_team` is true.\n\n" + HOUSE_RULES
    ),
    toolsets=[catalogue_tools, self_tools],
    capabilities=[guard, online("volunteer_onboarding")],
    model_settings=MODEL_SETTINGS,
    retries=RETRIES,
    defer_model_check=True,
)


class OrganisationInfo(BaseModel):
    org_id: str
    name: str
    description: str
    joined: bool
    onboarding_steps: list[str]  # what the organisation asks of a new volunteer, in order


@agent.tool
def list_organisations(ctx: Ctx) -> list[OrganisationInfo]:
    """Every organisation on the network, whether the volunteer already volunteers with it, and
    the onboarding steps it asks of a volunteer who joins (empty: none)."""
    deps = ctx.deps
    orgs = services.directory(deps.db, deps.actor_id)
    deps.project_now()
    steps: dict[str, list[str]] = {}
    for row in deps.graph.read(cypher.ONBOARDING_FLOWS, {"orgs": [o["id"] for o in orgs]}):
        steps.setdefault(row["org"], []).append(row["title"])
    return [
        OrganisationInfo(
            org_id=o["id"],
            name=o["name"],
            description=o["description"],
            joined=bool(o["opted_in"]),
            onboarding_steps=steps.get(o["id"], []),
        )  # fmt: skip
        for o in orgs
    ]


class WorkflowStep(BaseModel):
    step_id: str
    position: int
    title: str
    description: str
    completed_by: Literal["volunteer", "org_admin", "lead"]
    state: Literal["done", "current", "waiting"]


class OrganisationOnboarding(BaseModel):
    """One workflow the volunteer is in: an organisation's own (`project_id` empty), or that of
    one of its projects, for joining the project's team."""

    org_id: str
    organisation: str
    project_id: str = ""
    project: str = ""
    finished: bool
    on_team: bool = False  # projects only: the lead side has accepted them
    awaiting_acceptance: bool = False  # projects only: steps done, waiting for the lead side
    steps: list[WorkflowStep]


def graph_onboarding(deps: AgentDeps) -> list[OrganisationOnboarding]:
    """The volunteer's place in each organisation's workflow, and in the workflow of each
    project whose team they are joining, as the Relate graph holds them."""
    deps.project_now()  # anything just changed is in the graph before it is read
    found: dict[tuple[str, str], OrganisationOnboarding] = {}
    who = {"volunteer": deps.actor_id}
    rows = [
        *deps.graph.read(cypher.ONBOARDING_FOR_VOLUNTEER, who),
        *deps.graph.read(cypher.PROJECT_ONBOARDING_FOR_VOLUNTEER, who),
    ]
    for row in rows:
        project_id = row.get("project", "")
        run = found.setdefault(
            (row["org"], project_id),
            OrganisationOnboarding(
                org_id=row["org"],
                organisation=row["organisation"],
                project_id=project_id,
                project=row.get("project_title", ""),
                finished=True,
                on_team=bool(row.get("on_team", False)),
                steps=[],
            ),  # fmt: skip
        )
        state = "done" if row["done"] else "current" if row["current"] else "waiting"
        run.finished = run.finished and state == "done"
        run.steps.append(
            WorkflowStep(
                step_id=row["step"],
                position=row["position"],
                title=row["title"],
                description=row["description"],
                completed_by=row["completed_by"],
                state=state,
            )  # fmt: skip
        )
    for run in found.values():
        run.awaiting_acceptance = bool(run.project_id) and run.finished and not run.on_team
    return list(found.values())


def _current_steps(
    deps: AgentDeps,
) -> dict[tuple[str, str], tuple[OrganisationOnboarding, WorkflowStep]]:
    """The volunteer's current steps by (org_id, step_id), each with the workflow it is in."""
    return {
        (run.org_id, step.step_id): (run, step)
        for run in graph_onboarding(deps)
        for step in run.steps
        if step.state == "current"
    }


@agent.tool
def my_onboarding(ctx: Ctx) -> list[OrganisationOnboarding]:
    """The volunteer's onboarding with each organisation they joined that has its own steps,
    and with each project whose team they are joining (`project_id` set): every step in order,
    and whether it is done, their current step, or still waiting. Read from the graph. Empty:
    no organisation or project asks anything more of them."""
    return graph_onboarding(ctx.deps)


@agent.tool
def complete_onboarding_step(ctx: Ctx, org_id: str, step_id: str) -> str:
    """Tick off the volunteer's current step in an organisation's or a project's onboarding,
    after they approve. Only for a step whose state is `current` and that is completed by the
    volunteer.

    Args:
        org_id: The organisation, from my_onboarding.
        step_id: The step, from my_onboarding.
    """
    deps = ctx.deps
    run, step = _current_steps(deps).get((org_id, step_id), (None, None))
    if run is None or step is None:
        raise ModelRetry(
            "That is not the volunteer's current step. Use an org_id and step_id from "
            "my_onboarding whose state is 'current'."
        )
    if step.completed_by != "volunteer":
        confirmer = "the project lead" if step.completed_by == "lead" else "the organisation"
        raise ModelRetry(
            f"'{step.title}' is confirmed by {confirmer}, not by the volunteer. Tell them to "
            f"wait for {confirmer}."
        )
    if not ctx.tool_call_approved:
        raise ApprovalRequired(metadata={"summary": f"Mark as done: {step.title}"})
    result = services.onboarding.complete_step(
        deps.db, deps.actor_id, org_id, step_id, project_id=run.project_id
    )
    deps.project_now()
    if result["state"] != "completed":
        return f"Done. {result['steps_done']} of {result['steps_total']} steps are finished."
    if run.project_id:
        return (
            f"Done. They have finished the onboarding steps for '{result['project']}'. They "
            "are not on the team yet: the project lead accepts them."
        )
    return f"Done. Their onboarding with {result['organisation']} is finished."


def _merged(
    deps: AgentDeps, skills: dict[str, SkillState], causes: dict[str, CauseStance]
) -> tuple[services.Profile, dict[str, str], dict[str, str]]:
    """The profile as it would be after the change, or ModelRetry saying what is wrong."""
    catalogue = get_catalogue()
    if unknown := sorted(set(skills) - catalogue.skill_ids):
        raise ModelRetry(f"Not in the skill catalogue: {unknown}. Use IDs from list_skills.")
    if unknown := sorted(set(causes) - catalogue.cause_ids):
        raise ModelRetry(f"Not in the cause catalogue: {unknown}. Use IDs from list_causes.")
    current = services.get_profile(deps.db, deps.actor_id)
    locked = sorted(
        [s for s, state in skills.items() if current.skills.get(s) == "exclude" != state]
        + [c for c, st in causes.items() if current.causes.get(c) == "exclude" != st]
    )
    if locked:
        raise ModelRetry(
            f"The volunteer has excluded {locked}. Leave those exactly as they are; only they "
            "can lift an exclusion, on their profile page."
        )
    return current, current.skills | skills, current.causes | causes


@agent.tool
def save_profile(ctx: Ctx, skills: dict[str, SkillState], causes: dict[str, CauseStance]) -> str:
    """Save skills and cause preferences to the volunteer's profile, after they approve.

    Only the entries given are changed; everything else on the profile stays as it is.

    Args:
        skills: skill_id -> has | offers | exclude.
        causes: cause_id -> prefer | exclude.
    """
    deps = ctx.deps
    if not skills and not causes:
        raise ModelRetry("Give at least one skill or cause to save.")
    current, new_skills, new_causes = _merged(deps, skills, causes)
    if not ctx.tool_call_approved:
        names = get_catalogue()
        changes = [f"{names.skill_name(s)}: {state}" for s, state in sorted(skills.items())]
        changes += [f"{names.cause_name(c)}: {stance}" for c, stance in sorted(causes.items())]
        raise ApprovalRequired(metadata={"summary": "Save to your profile: " + "; ".join(changes)})
    services.update_profile(
        deps.db, deps.actor_id, display_name=current.display_name, skills=new_skills,
        causes=new_causes, expected_version=current.version,
    )  # fmt: skip
    deps.project_now()
    return (
        f"Saved. The profile now offers {sum(s == 'offers' for s in new_skills.values())} skill(s)."
    )


@agent.tool
def submit_for_vetting(ctx: Ctx) -> str:
    """Send the volunteer's profile to a moderator for screening, after they approve. The
    profile must offer at least one skill, and must not already be in screening or active."""
    deps = ctx.deps
    actor = services.get_actor(deps.db, deps.actor_id)
    if actor.status not in services.SUBMITTABLE:
        raise ModelRetry(
            f"Their status is '{services.VOLUNTEER_STATUS_LABELS[actor.status]}', so there is "
            "nothing to submit. Tell them where they stand instead."
        )
    if "offers" not in services.get_profile(deps.db, deps.actor_id).skills.values():
        raise ModelRetry("The saved profile offers no skill yet. Save one first.")
    if not ctx.tool_call_approved:
        raise ApprovalRequired(
            metadata={"summary": "Send your profile to a moderator for vetting."}
        )
    services.submit_for_vetting(deps.db, deps.actor_id)
    return "Submitted. The profile is now in screening and a moderator has been told."


@agent.output_validator
def _grounded(ctx: Ctx, advice: OnboardingAdvice | DeferredToolRequests):
    if not isinstance(advice, OnboardingAdvice):
        return advice
    deps = ctx.deps
    _merged(
        deps,
        {s.skill_id: s.state for s in advice.skills},
        {c.cause_id: c.stance for c in advice.causes},
    )
    known = {o["id"] for o in services.directory(deps.db, deps.actor_id)}
    if unknown := sorted({o.org_id for o in advice.organisations} - known):
        raise ModelRetry(f"Unknown organisations: {unknown}. Use IDs from list_organisations.")
    if advice.next_steps:
        current = _current_steps(deps)
        if wrong := sorted({(s.org_id, s.step_id) for s in advice.next_steps} - set(current)):
            raise ModelRetry(
                f"Not the volunteer's current step: {wrong}. `next_steps` may only name steps "
                "that my_onboarding marks 'current'."
            )
    offers = "offers" in services.get_profile(deps.db, deps.actor_id).skills.values()
    status = services.get_actor(deps.db, deps.actor_id).status
    ready = offers and status in services.SUBMITTABLE
    if advice.ready_for_vetting and not ready:
        raise ModelRetry(
            "ready_for_vetting must be false: the saved profile offers no skill yet, or the "
            f"account is already '{status}'."
        )
    return advice


assistant = register(
    Assistant(
        name="volunteer-onboarding",
        title="Onboarding assistant",
        audience="Any signed-in volunteer, for their own profile",
        description=(
            "Tell it what you can do and care about. It proposes skills and causes from the "
            "catalogue, and saves them or sends your profile for vetting once you approve. It "
            "also guides you through each organisation's own onboarding steps, and those of "
            "the projects whose teams you are joining."
        ),
        agent=agent,
    )
)
