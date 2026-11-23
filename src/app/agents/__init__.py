"""The assistants: Pydantic AI agents for onboarding volunteers, onboarding projects, matching
and auditing.

| Assistant | For | Changes anything? |
|---|---|---|
| `volunteer-onboarding` | a volunteer, their own profile | saves the profile, submits for vetting: each after approval |
| `project-scoping` | an active volunteer, for their organisation | saves a private draft, after approval |
| `project-review` | the organisation's reviewers | no |
| `match-shortlist` | a project's lead side | sends an invitation, after approval |
| `match-explainer` | a volunteer, their own matches | no |
| `audit` | system admins | no |

Every one of them is run through app.agents.runner, which authorises, limits, records and traces
the run. What they share (dependencies, the tool guard, house rules) is in app.agents.deps.
Importing this package registers them.
"""  # noqa: E501

from app.agents import audit, match, onboarding, projects
from app.agents.runner import (
    REGISTRY,
    AgentFailed,
    AgentsDisabled,
    Assistant,
    QuotaExceeded,
    Run,
    get_run,
    my_runs,
    resume,
    start,
)

__all__ = [
    "REGISTRY",
    "AgentFailed",
    "AgentsDisabled",
    "Assistant",
    "QuotaExceeded",
    "Run",
    "audit",
    "get_run",
    "match",
    "my_runs",
    "onboarding",
    "projects",
    "resume",
    "start",
]
