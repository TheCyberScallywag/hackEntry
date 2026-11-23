"""Offline evaluations: each assistant against the seeded demo, with a real model.

    uv run python -m app.cli evals                 # every assistant, the configured AGENT_MODEL
    uv run python -m app.cli evals --agent audit --judge

It builds a disposable copy of the demo, runs each dataset's cases through the same runner the
API uses (so authorisation, limits, the audit trail and tracing are all exercised), and scores
every run. With LOGFIRE_TOKEN set, each dataset is an experiment in Logfire, with the trace of
every case. The command exits 1 if any assertion fails, so it can gate a model or prompt change.

The cases only ever stop at an approval; none is approved, so a run changes nothing and the cases
are independent of each other.

Scoring is deterministic first (`Expectations`, `NoContactDetails`, `ReasonsGiven`, and limits on
requests and tool calls). `--judge` adds an LLM judge per assistant for what code cannot check:
tone, and whether the explanation follows from the data.
"""

import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import LLMJudge, MaxModelRequests, MaxToolCalls
from pydantic_evals.reporting import EvaluationReport

from app import agents, services
from app.agents.evaluators import Expectations, NoContactDetails, ReasonsGiven
from app.catalogue import stable_id
from app.config import Settings
from app.runtime import Runtime, reset_demo


def _u(key: str) -> str:
    return stable_id("u", key)


DK, CS = stable_id("org", "datakind-uk"), stable_id("org", "cyber-scallywags")
FOOD_BANK = stable_id("p", "food-bank-dashboard")
VAGUE_TITLE = "Help people"  # the suggestion `prepare` adds for the review assistant

RUBRICS = {
    "volunteer-onboarding": (
        "The advice is addressed to the volunteer, is warm and plain, proposes only skills the "
        "volunteer's message supports, and does not promise that they will be approved."
    ),
    "project-scoping": (
        "The summary says what will be done, for whom, and what finished looks like, using only "
        "what the author said. Anything assumed is listed as an open question."
    ),
    "project-review": (
        "The reason for the author is specific and kind, and says what to change if a change is "
        "asked for. The recommendation follows from the checks."
    ),
    "match-shortlist": (
        "Each recommendation says which skills the person brings and why them. No one is "
        "promised a place and no gap is glossed over."
    ),
    "match-explainer": (
        "The explanation is addressed to the volunteer, does not promise a place on a team, "
        "and never suggests lifting an exclusion."
    ),
    "audit": (
        "Every finding has a concrete, proportionate recommendation an admin could act on, and "
        "the usage notes quote figures from the data."
    ),
}


@dataclass(frozen=True)
class Suite:
    assistant: str
    cases: list[Case]


def prepare(rt: Runtime) -> dict[str, str]:
    """Add what the cases need beyond the demo seed. Returns the IDs they refer to."""
    vague = services.create_submission(
        rt.db, _u("asha"), DK,
        services.Draft(VAGUE_TITLE, "Do some things to help people out.", "homelessness",
                       ["fundraising", "carpentry", "web-development"], []),
    )  # fmt: skip
    return {"vague": vague}


def suites(ids: dict[str, str]) -> list[Suite]:
    return [
        Suite(
            "volunteer-onboarding",
            [
                Case(
                    name="new-volunteer-two-skills",
                    inputs={
                        "as": "rhiannon",
                        "prompt": "I keep the books for a small charity "
                        "and I'd like to help with fundraising too.",
                    },
                    metadata={"offers": ["bookkeeping", "fundraising"], "approval": "save_profile"},
                ),  # fmt: skip
                Case(
                    name="respects-an-exclusion",
                    inputs={
                        "as": "gwen",
                        "prompt": "People say I'm a great listener. I'm also "
                        "a trained first aider and happy to do that for a project.",
                    },
                    metadata={
                        "offers": ["first-aid"],
                        "untouched": ["active-listening"],
                        "approval": "save_profile",
                    },
                ),  # fmt: skip
                Case(
                    name="too-little-to-go-on",
                    inputs={"as": "rhiannon", "prompt": "Hi, I'd like to volunteer."},
                    metadata={"approval": None},
                ),
            ],
        ),
        Suite(
            "project-scoping",
            [
                Case(
                    name="scam-awareness-drop-in",
                    inputs={
                        "as": "asha",
                        "org_id": CS,
                        "prompt": "A weekly drop-in at the "
                        "library where we show older people how to spot scam emails and "
                        "texts. Six sessions, about ten people each.",
                    },
                    metadata={
                        "cause": "digital-inclusion",
                        "required": ["security-awareness"],
                        "approval": "save_draft",
                    },
                ),  # fmt: skip
            ],
        ),
        Suite(
            "project-review",
            [
                Case(
                    name="vague-suggestion-needs-scoping",
                    inputs={
                        "as": "rhiannon",
                        "org_id": DK,
                        "subject_id": ids["vague"],
                        "prompt": "Assess this suggestion.",
                    },
                    metadata={"recommendation": "changes_requested"},
                ),  # fmt: skip
            ],
        ),
        Suite(
            "match-shortlist",
            [
                Case(
                    name="cover-the-data-analysis-gap",
                    inputs={
                        "as": "cerys",
                        "subject_id": FOOD_BANK,
                        "prompt": "Who should I invite?",
                    },
                    metadata={
                        "recommends": [_u("dev")],
                        "only": [_u("asha"), _u("dev"), _u("kofi")],
                        "approval": None,
                    },
                ),  # fmt: skip
            ],
        ),
        Suite(
            "match-explainer",
            [
                Case(
                    name="explain-a-skills-match",
                    inputs={"as": "asha", "org_id": DK, "prompt": "Explain my matches."},
                    metadata={"explains": [FOOD_BANK]},
                ),
            ],
        ),
        Suite(
            "audit",
            [
                Case(
                    name="demo-seed-audit",
                    inputs={"as": "sam", "prompt": ""},
                    metadata={
                        "cites": [
                            "single-system-admin",
                            f"org-without-admin:{stable_id('org', 'interlinkrct')}",
                        ],
                        "overall": "attention",
                    },
                ),  # fmt: skip
            ],
        ),
    ]


def task_for(rt: Runtime, assistant: str) -> Callable[[dict], dict]:
    """The function a dataset evaluates: one run through the runner, as the case's user."""

    def run(inputs: dict) -> dict:
        result = agents.start(
            rt, assistant, actor_id=_u(inputs["as"]), prompt=inputs["prompt"],
            org_id=inputs.get("org_id"), subject_id=inputs.get("subject_id", ""),
        )  # fmt: skip
        return {"status": result.status, "output": result.output, "pending": result.pending}

    run.__name__ = assistant.replace("-", "_")
    return run


def dataset(rt: Runtime, suite: Suite, *, judge: bool = False) -> Dataset:
    limits = rt.settings
    evaluators = [
        Expectations(),
        NoContactDetails(),
        ReasonsGiven(),
        MaxModelRequests(limits.agent_request_limit),
        MaxToolCalls(limits.agent_tool_calls_limit),
    ]
    if judge:
        evaluators.append(
            LLMJudge(rubric=RUBRICS[suite.assistant], model=rt.agent_model, include_input=True)
        )
    return Dataset(name=suite.assistant, cases=suite.cases, evaluators=evaluators)


def failed(report: EvaluationReport) -> list[str]:
    """Every assertion that did not hold and every case that could not run, as `case: what`."""
    problems = [f"{f.name}: {f.error_message}" for f in report.failures]
    for case in report.cases:
        problems += [f"{case.name}: {name}" for name, r in case.assertions.items() if not r.value]
        problems += [f"{case.name}: {f.name} raised" for f in case.evaluator_failures]
    return problems


def run(
    rt: Runtime, *, only: str = "", judge: bool = False, progress: bool = False
) -> dict[str, EvaluationReport]:
    """Evaluate the assistants (or just `only`) against `rt`, which must hold the demo seed."""
    ids = prepare(rt)
    reports = {}
    for suite in suites(ids):
        if only and suite.assistant != only:
            continue
        reports[suite.assistant] = dataset(rt, suite, judge=judge).evaluate_sync(
            task_for(rt, suite.assistant), max_concurrency=1, progress=progress
        )
    return reports


def demo_runtime(settings: Settings) -> Runtime:
    """A fresh demo in a temporary directory, with the caller's model and limits. The quota is
    lifted: every case runs as one of a handful of demo accounts."""
    scratch = Path(tempfile.mkdtemp(prefix="vn-evals-"))
    return reset_demo(settings.model_copy(update={"data_dir": scratch, "agent_daily_runs": 0}))
