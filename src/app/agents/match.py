"""Matching assistants. The Relate graph decides who is eligible; these explain and advise.

**Shortlist** (`match-shortlist`) is for the lead side of one project: its lead, or an admin of
its organisation. It reads the ranked candidates the graph produced, works out which required
skills the team does not cover yet, and recommends who to invite and why. It can send an
invitation once the lead approves it.

**Explainer** (`match-explainer`) is for a volunteer, about their own matches in one
organisation: why each project fits, what is blocking the ones that do not, and what they could
change.

Neither can widen the pool. The output validators re-read the candidates and matches from the
application and reject any person or project the graph did not return, and the shortlist can only
credit a candidate with skills the graph matched them on. Names in the output come from the
database, not from the model.
"""

from pydantic import BaseModel, Field
from pydantic_ai import Agent, ApprovalRequired, DeferredToolRequests, ModelRetry

from app import matching, placements, services
from app.agents.deps import HOUSE_RULES, MODEL_SETTINGS, RETRIES, AgentDeps, Ctx, guard, self_tools
from app.agents.evaluators import online
from app.agents.runner import Assistant, register
from app.catalogue import get_catalogue
from app.graph import cypher


# ---------------------------------------------------------------- shortlist (the lead side)
class Candidate(BaseModel):
    volunteer_id: str
    name: str
    status: str  # candidate | invited | team | declined | left | removed | lapsed
    prefers_cause: bool
    required_skills_covered: list[str]
    optional_skills_offered: list[str]
    can_invite: bool


class ProjectPicture(BaseModel):
    title: str
    summary: str
    cause: str
    required_skills: list[str]
    optional_skills: list[str]
    team_covers: list[str]
    not_yet_covered: list[str]
    people: list[Candidate] = Field(description="In the graph's ranked order")


def _picture(deps: AgentDeps) -> ProjectPicture:
    """The project, its team and its candidates, as this actor may see them (lead side only)."""
    rows = placements.for_project(deps.db, deps.actor_id, deps.subject_id)  # checks the role
    project = matching.project(
        deps.db, deps.graph, deps.actor_id, deps.org_id or "", deps.subject_id
    )
    # What the team covers comes from the graph's ON_TEAM edges, as on the coverage screen: a
    # placement row only records the skills someone was matched on, and a lead joins their own
    # team without a match. Only people SQLite still has on the team count.
    on_team = {r["volunteer_id"] for r in rows if r["status"] == "team"}
    names = get_catalogue()
    covered = {
        names.skill_name(row["skill"])
        for row in deps.graph.read(
            cypher.TEAM_COVERAGE, {"org": deps.org_id or "", "project": deps.subject_id}
        )
        if on_team & set(row["team"])
    }
    return ProjectPicture(
        title=project["title"],
        summary=project["summary"],
        cause=project["cause_name"],
        required_skills=project["required_names"],
        optional_skills=project["optional_names"],
        team_covers=sorted(covered),
        not_yet_covered=[s for s in project["required_names"] if s not in covered],
        people=[
            Candidate(
                volunteer_id=r["volunteer_id"],
                name=r["display_name"],
                status=r["status"],
                prefers_cause=r["preferred"],
                required_skills_covered=r["matched_skills"],
                optional_skills_offered=r["optional_skills"],
                can_invite=r["status"] == "candidate" and r["can_decide"],
            )
            for r in rows
        ],
    )


class Recommendation(BaseModel):
    volunteer_id: str
    name: str
    covers: list[str] = Field(description="Skill names this person would bring")
    reason: str = Field(min_length=8)


class Shortlist(BaseModel):
    """Who to invite, in order, and what would still be missing."""

    summary: str = Field(min_length=10)
    recommendations: list[Recommendation] = Field(default_factory=list)
    still_uncovered: list[str] = Field(
        default_factory=list,
        description="Required skills nobody on the team or the shortlist covers",
    )


shortlist_agent: Agent[AgentDeps, Shortlist | DeferredToolRequests] = Agent(
    None,
    name="match_shortlist",
    deps_type=AgentDeps,
    output_type=[Shortlist, DeferredToolRequests],
    instructions=(
        "You help a project lead decide who to invite to their team.\n\n"
        "Read the project (project_picture). The people listed are the only people there are, "
        "and they are already in the graph's ranked order: preferred cause first, then required "
        "skills covered, then optional skills.\n"
        "Recommend from people whose can_invite is true. Prefer the smallest set that covers "
        "the skills in not_yet_covered; break ties by the ranked order. Do not recommend "
        "someone who adds nothing the team lacks unless nobody does. For each, say which "
        "skills they bring and why them. List in still_uncovered any required skill that "
        "nobody on the team or your shortlist covers: that is a gap the lead needs to know "
        "about, not something to paper over.\n"
        "Call invite only when the lead asks you to invite someone, and only for the people "
        "they mean. It waits for their approval.\n\n" + HOUSE_RULES
    ),
    capabilities=[guard, online("match_shortlist")],
    model_settings=MODEL_SETTINGS,
    retries=RETRIES,
    defer_model_check=True,
)


@shortlist_agent.tool
def project_picture(ctx: Ctx) -> ProjectPicture:
    """The project, what its team already covers, what is not covered yet, and every candidate
    and team member in ranked order."""
    return _picture(ctx.deps)


@shortlist_agent.tool
def invite(ctx: Ctx, volunteer_id: str, note: str = "") -> str:
    """Invite one candidate to the team, after the lead approves. The volunteer is notified and
    joins when they say yes.

    Args:
        volunteer_id: A volunteer_id from project_picture whose can_invite is true.
        note: An optional short note recorded with the invitation.
    """
    deps = ctx.deps
    person = next((p for p in _picture(deps).people if p.volunteer_id == volunteer_id), None)
    if person is None:
        raise ModelRetry("That is not one of this project's candidates. Use project_picture.")
    if not person.can_invite:
        raise ModelRetry(f"{person.name} cannot be invited: their status is '{person.status}'.")
    if not ctx.tool_call_approved:
        raise ApprovalRequired(metadata={"summary": f"Invite {person.name} to the team."})
    with deps.db.read() as conn:
        version = conn.execute(
            "SELECT version FROM placements WHERE project_id = ? AND volunteer_id = ?",
            (deps.subject_id, volunteer_id),
        ).fetchone()[0]
    placements.lead_respond(
        deps.db, deps.actor_id, deps.subject_id, volunteer_id, "yes", version=version, note=note
    )
    deps.project_now()
    return f"Invited {person.name}. They join the team once they say yes."


@shortlist_agent.output_validator
def _only_real_candidates(ctx: Ctx, shortlist: Shortlist | DeferredToolRequests):
    if not isinstance(shortlist, Shortlist):
        return shortlist
    picture = _picture(ctx.deps)
    people = {p.volunteer_id: p for p in picture.people}
    seen: set[str] = set()
    for rec in shortlist.recommendations:
        person = people.get(rec.volunteer_id)
        if person is None or rec.volunteer_id in seen:
            raise ModelRetry(
                f"{rec.volunteer_id} is not a candidate for this project, or is listed twice. "
                "Recommend only people from project_picture, once each."
            )
        seen.add(rec.volunteer_id)
        if person.status not in ("candidate", "invited"):
            raise ModelRetry(f"{person.name} is '{person.status}', not a candidate.")
        offered = set(person.required_skills_covered) | set(person.optional_skills_offered)
        if extra := sorted(set(rec.covers) - offered):
            raise ModelRetry(
                f"{person.name} was not matched on {extra}. In covers, list only skills from "
                "their required_skills_covered or optional_skills_offered."
            )
        rec.name = person.name  # from the database, whatever the model wrote
    covered = set(picture.team_covers) | {s for r in shortlist.recommendations for s in r.covers}
    # Computed, not trusted: a gap must not disappear because the model forgot it.
    shortlist.still_uncovered = [s for s in picture.required_skills if s not in covered]
    return shortlist


def _may_shortlist(deps: AgentDeps) -> None:
    with deps.db.read() as conn:
        info = placements._check_lead(conn, deps.actor_id, deps.subject_id)  # noqa: SLF001
    deps.org_id = info["org_id"]


shortlist = register(
    Assistant(
        name="match-shortlist",
        title="Shortlist assistant",
        audience="The project's lead, or an admin of its organisation",
        description=(
            "Recommends which of the graph's candidates to invite so the team covers the "
            "project's required skills, and names any gap that would remain. Sends an invitation "
            "once you approve it."
        ),
        agent=shortlist_agent,
        authorise=_may_shortlist,
    )
)


# ---------------------------------------------------------------- explainer (the volunteer)
class MatchInfo(BaseModel):
    project_id: str
    title: str
    cause: str
    prefers_cause: bool
    skills_you_bring: list[str]
    optional_skills_you_bring: list[str]
    skills_still_needed_from_others: list[str]
    where_it_stands: str | None  # invited, on the team, declined... or None: just a match


class BlockedInfo(BaseModel):
    project_id: str
    title: str
    reasons: list[str]


class MatchPicture(BaseModel):
    organisation: str
    withheld: str | None = Field(description="Why no results are shown right now, if so")
    matches: list[MatchInfo]
    blocked_by_your_exclusions: list[BlockedInfo]


def _matches(deps: AgentDeps) -> MatchPicture:
    result = matching.volunteer_matches(deps.db, deps.graph, deps.actor_id, [deps.org_id or ""])
    with deps.db.read() as conn:
        org = services.get_org(conn, deps.org_id or "")
    return MatchPicture(
        organisation=org["name"],
        withheld=result.withheld.reason if result.withheld else None,
        matches=[
            MatchInfo(
                project_id=m["project"],
                title=m["title"],
                cause=m["cause_name"],
                prefers_cause=m["preferred"],
                skills_you_bring=m["matched_names"],
                optional_skills_you_bring=m["optional_names"],
                skills_still_needed_from_others=m["remaining_names"],
                where_it_stands=m["placement_label"],
            )
            for m in result.matches
        ],
        blocked_by_your_exclusions=[
            BlockedInfo(project_id=e["project"], title=e["title"], reasons=e["reasons"])
            for e in result.excluded
        ],
    )


class MatchNote(BaseModel):
    project_id: str
    title: str
    why: str = Field(min_length=8, description="Why it fits them, in one or two sentences")


class BlockedNote(BaseModel):
    project_id: str
    title: str
    why: str = Field(min_length=8)


class ElsewhereInfo(BaseModel):
    organisation: str
    skill_needed: str
    cause: str
    prefers_cause: bool
    projects: int


class ElsewhereNote(BaseModel):
    organisation: str
    skill_needed: str
    why: str = Field(min_length=8)


class MatchExplanation(BaseModel):
    """A volunteer's matches in one organisation, explained to them."""

    summary: str = Field(min_length=10)
    matches: list[MatchNote] = Field(default_factory=list)
    blocked: list[BlockedNote] = Field(
        default_factory=list, description="Projects their own exclusions rule out"
    )
    elsewhere: list[ElsewhereNote] = Field(
        default_factory=list,
        description="Organisations they have not joined that need a skill they offer "
        "(where_else_am_i_needed)",
    )
    next_steps: list[str] = Field(
        default_factory=list, description="Things they could do; never 'lift an exclusion'"
    )


explainer_agent: Agent[AgentDeps, MatchExplanation] = Agent(
    None,
    name="match_explainer",
    deps_type=AgentDeps,
    output_type=MatchExplanation,
    instructions=(
        "You explain to a volunteer how they match their organisation's projects.\n\n"
        "Read their matches (my_matches) and, if useful, their profile (my_profile). For each "
        "match say why it fits: the skills they bring and whether it is a cause they prefer. "
        "Being a match is not an invitation; the project lead chooses who to invite, so do "
        "not promise a place. For each project their own exclusions rule out, say which "
        "exclusion, neutrally: an exclusion is a choice they made and it is respected, so never "
        "suggest lifting one. If results are withheld, say why in the summary and return no "
        "matches. If there are no matches, say so honestly and suggest next steps such as "
        "adding skills they have but did not list, or joining another organisation.\n\n"
        "where_else_am_i_needed asks the graph which organisations they have not joined need a "
        "skill they offer. Use it when they have few matches or ask what else they could do, "
        "and list what it returns under elsewhere. Joining is their choice, and it is not an "
        "invitation. It never returns a person, so name none.\n\n" + HOUSE_RULES
    ),
    toolsets=[self_tools],
    capabilities=[guard, online("match_explainer")],
    model_settings=MODEL_SETTINGS,
    retries=RETRIES,
    defer_model_check=True,
)


@explainer_agent.tool
def my_matches(ctx: Ctx) -> MatchPicture:
    """The volunteer's matches in this organisation, as the graph decided them, and the projects
    their own exclusions rule out."""
    return _matches(ctx.deps)


def _elsewhere(deps: AgentDeps) -> list[ElsewhereInfo]:
    result = matching.needed_elsewhere(deps.db, deps.graph, deps.actor_id)
    return [
        ElsewhereInfo(
            organisation=o["org_name"],
            skill_needed=o["skill_name"],
            cause=o["cause_name"],
            prefers_cause=o["preferred"],
            projects=o["projects"],
        )
        for o in result.opportunities
    ]


@explainer_agent.tool
def where_else_am_i_needed(ctx: Ctx) -> list[ElsewhereInfo]:
    """Organisations across the network that the volunteer has NOT joined, with a project that
    needs a skill they offer and has nobody on its team covering it yet. Their exclusions are
    already applied. Organisations, skills and causes only. Empty: nothing to suggest."""
    return _elsewhere(ctx.deps)


@explainer_agent.output_validator
def _only_real_matches(ctx: Ctx, explanation: MatchExplanation) -> MatchExplanation:
    picture = _matches(ctx.deps)
    matches = {m.project_id: m.title for m in picture.matches}
    blocked = {b.project_id: b.title for b in picture.blocked_by_your_exclusions}
    for kind, notes, known in (
        ("match", explanation.matches, matches),
        ("blocked project", explanation.blocked, blocked),
    ):
        for note in notes:
            if note.project_id not in known:
                raise ModelRetry(
                    f"{note.project_id} is not a {kind} from my_matches. Explain only what the "
                    "tool returned."
                )
            note.title = known[note.project_id]
    if explanation.elsewhere:
        real = {(e.organisation, e.skill_needed) for e in _elsewhere(ctx.deps)}
        for note in explanation.elsewhere:
            if (note.organisation, note.skill_needed) not in real:
                raise ModelRetry(
                    f"{note.organisation} needing {note.skill_needed} is not something "
                    "where_else_am_i_needed returned. List only what the tool returned."
                )
    if missing := sorted(set(matches) - {m.project_id for m in explanation.matches}):
        raise ModelRetry(f"Explain every match; these are missing: {missing}.")
    return explanation


def _may_explain(deps: AgentDeps) -> None:
    services.require_access(deps.db, deps.actor_id, deps.org_id or "", role="volunteer")


explainer = register(
    Assistant(
        name="match-explainer",
        title="Match explainer",
        audience="A volunteer, for an organisation they volunteer with",
        description=(
            "Explains why each project matches you, which projects your own exclusions rule "
            "out, and what you could do next."
        ),
        agent=explainer_agent,
        authorise=_may_explain,
    )
)
