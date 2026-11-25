"""What every assistant shares: its dependencies, the model, the tool guard and common tools.

The rules that make the assistants safe live here, once:

* **The acting user comes from the session, never from the model.** `AgentDeps.actor_id` is set
  by the server; no tool takes a user ID for "who am I", so a prompt cannot make an assistant act
  as someone else.
* **Tools call the same service functions as the pages and the API**, with that actor ID, so
  every role check runs again inside the tool. An assistant can do nothing its user could not.
* **Writes wait for approval.** A tool that changes something validates its arguments, then
  raises `ApprovalRequired`; the run stops and the user sees exactly what would be done.
* **Domain errors become something the model can use** (`guard`): invalid input is sent back for
  another try, a refusal is reported as a refusal and audited, and nothing else is swallowed.
"""

import contextlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel
from pydantic_ai import FunctionToolset, ModelRetry, ModelSettings, RunContext
from pydantic_ai.capabilities import Hooks
from pydantic_ai.models import Model, infer_model
from pydantic_ai.models.fallback import FallbackModel

from app import audit, services
from app.catalogue import get_catalogue, text_query
from app.config import Settings
from app.graph import cypher
from app.graph.store import GraphError, GraphStore
from app.sqlstore import Database

if TYPE_CHECKING:
    from app.runtime import Runtime

# Short answers: every output is a small structured object, never an essay.
MODEL_SETTINGS = ModelSettings(max_tokens=2500)
RETRIES = 2

HOUSE_RULES = """\
Rules that always apply:
- Use only identifiers returned by your tools. Never invent a skill, cause, organisation, \
project or person.
- The matching graph decides who is eligible for what. You explain and suggest; you never \
decide eligibility yourself.
- A skill or cause someone has excluded is a hard boundary. Never suggest changing it.
- Refer to people by display name only. Never ask for, guess or repeat contact details.
- A tool that changes something stops and waits for the user's approval. Call it once, with \
complete arguments, and do not tell the user it is done until the tool says so.
- If a tool answers REFUSED, the user is not allowed to do that. Say so plainly and stop.
- Write plain British English, short sentences, no jargon."""


@dataclass
class AgentDeps:
    """One run's dependencies. Built by the server for the signed-in user."""

    rt: Runtime
    actor_id: str
    run_id: str = ""
    org_id: str | None = None
    subject_id: str = ""  # the project or submission the run is about
    days: int = 7  # the audit assistant's period
    facts: dict[str, Any] = field(default_factory=dict)  # gathered before the model is asked
    tools_used: list[str] = field(default_factory=list)

    @property
    def db(self) -> Database:
        return self.rt.db

    @property
    def graph(self) -> GraphStore:
        return self.rt.graph

    @property
    def settings(self) -> Settings:
        return self.rt.settings

    def project_now(self) -> None:
        """Apply a write to the graph straight away; a failure stays queued for the projector."""
        with contextlib.suppress(Exception):  # the background loop retries
            self.rt.projector.drain()


Ctx = RunContext[AgentDeps]


# ---------------------------------------------------------------- the model
def build_model(settings: Settings) -> Model | None:
    """The configured model (with its fallback), or None when the assistants are switched off."""
    if not settings.agents_enabled:
        return None
    primary = _model(settings.agent_model, settings)
    if settings.agent_fallback_model:
        return FallbackModel(primary, _model(settings.agent_fallback_model, settings))
    return primary


def _model(name: str, settings: Settings) -> Model:
    key = settings.anthropic_api_key.get_secret_value()
    if name.startswith("anthropic:") and key:  # a key from .env, which is not in os.environ
        from pydantic_ai.models.anthropic import AnthropicModel
        from pydantic_ai.providers.anthropic import AnthropicProvider

        return AnthropicModel(name.split(":", 1)[1], provider=AnthropicProvider(api_key=key))
    return infer_model(name)


# ---------------------------------------------------------------- the tool guard
guard: Hooks[AgentDeps] = Hooks()


@guard.on.after_tool_execute
def _note_tool(ctx: Ctx, *, call, tool_def, args, result):
    ctx.deps.tools_used.append(call.tool_name)
    return result


@guard.on.tool_execute_error
def _domain_errors(ctx: Ctx, *, call, tool_def, args, error: Exception):
    """Turn a domain error into a tool result. Anything unexpected is raised unchanged."""
    deps = ctx.deps
    if isinstance(error, services.Invalid):
        raise ModelRetry(str(error)) from error
    if isinstance(error, services.Forbidden | services.NotFound):
        deps.tools_used.append(call.tool_name)
        audit.record_now(
            deps.db, "agent.tool_denied", actor_id=deps.actor_id, outcome="denied",
            target_kind="tool", target_id=call.tool_name, org_id=deps.org_id, source="agent",
            detail={"run_id": deps.run_id, "agent": ctx.agent.name if ctx.agent else ""},
        )  # fmt: skip
        return "REFUSED: the signed-in user is not allowed to do that, or it does not exist."
    if isinstance(error, services.Conflict):
        return f"CONFLICT: {error} Read the current state again before trying once more."
    if isinstance(error, GraphError):
        return "UNAVAILABLE: the matching graph could not answer just now."
    raise error


# ---------------------------------------------------------------- tools more than one agent uses
class SkillInfo(BaseModel):
    skill_id: str
    name: str
    category: str  # the skill area it sits in
    broader_area: str = ""
    kind: str = ""  # technical | practical | interpersonal | professional | language
    also_known_as: list[str] = []
    related: list[str] = []  # IDs of adjacent skills: close to this one, never a substitute


class CauseInfo(BaseModel):
    cause_id: str
    name: str


catalogue_tools: FunctionToolset[AgentDeps] = FunctionToolset()


@catalogue_tools.tool_plain
def list_skills() -> list[SkillInfo]:
    """Every skill in the shared catalogue, with its area, its other names and the skills
    adjacent to it. These are the only valid skill IDs."""
    cat = get_catalogue()
    areas = {a.id: a for a in cat.areas}
    return [
        SkillInfo(
            skill_id=s.id,
            name=s.name,
            category=s.category,
            broader_area=areas[areas[s.area].parent].name,
            kind=s.kind,
            also_known_as=list(s.alt_labels),
            related=list(cat.related_to(s.id)),
        )
        for s in cat.skills
    ]


@catalogue_tools.tool
def find_skills(ctx: Ctx, words: str) -> list[SkillInfo]:
    """Catalogue skills that match plain words someone used, such as "excel" or "dashboards",
    best match first. Searches each skill's name and its other names in the graph. Empty: no
    catalogue skill matches, so say so; never invent a skill ID."""
    query = text_query(words)
    if not query:
        return []
    return [
        SkillInfo(
            skill_id=row["skill"],
            name=row["name"],
            category=row["area"],
            broader_area=row["broader_area"],
            kind=row["kind"],
            also_known_as=list(get_catalogue().skill(row["skill"]).alt_labels),
            related=row["related"],
        )
        for row in ctx.deps.graph.read(cypher.FIND_SKILLS, {"query": query})
    ]


@catalogue_tools.tool_plain
def list_causes() -> list[CauseInfo]:
    """Every cause in the shared catalogue. These are the only valid cause IDs."""
    return [CauseInfo(cause_id=c.id, name=c.name) for c in get_catalogue().causes]


class ProfileInfo(BaseModel):
    display_name: str
    status: str
    status_label: str
    can_submit_for_vetting: bool
    skills: dict[str, str]  # skill_id -> none | has | offers | exclude (unset skills left out)
    causes: dict[str, str]  # cause_id -> prefer | exclude
    wants_to_learn: list[str] = []  # skill IDs; a wish, never a reason to be matched
    volunteers_with: list[str]  # organisation names


def read_profile(deps: AgentDeps) -> ProfileInfo:
    profile = services.get_profile(deps.db, deps.actor_id)
    actor = services.get_actor(deps.db, deps.actor_id)
    orgs = services.directory(deps.db, deps.actor_id)
    return ProfileInfo(
        display_name=profile.display_name,
        status=actor.status,
        status_label=services.VOLUNTEER_STATUS_LABELS[actor.status],
        can_submit_for_vetting=actor.status in services.SUBMITTABLE,
        skills=profile.skills,
        causes=profile.causes,
        wants_to_learn=profile.learning,
        volunteers_with=[o["name"] for o in orgs if o["opted_in"]],
    )


self_tools: FunctionToolset[AgentDeps] = FunctionToolset()


@self_tools.tool
def my_profile(ctx: Ctx) -> ProfileInfo:
    """The signed-in volunteer's own profile: skills, cause preferences, onboarding status and
    the organisations they volunteer with."""
    return read_profile(ctx.deps)
