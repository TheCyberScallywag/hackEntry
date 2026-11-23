"""Evaluators for the assistants, used two ways.

**Online** (`online`): two cheap, deterministic checks run on every live run, in the background,
and their results go to Logfire as `gen_ai.evaluation.result` events on the run's trace. They
need no model, so they cost nothing and can run on all traffic:

* `NoContactDetails`: the output holds no email address or phone number;
* `ReasonsGiven`: every suggestion carries a reason a person can check.

**Offline** (app.agents.evals): the same two, plus `Expectations`, which compares a run with what
a dataset case says must happen (which skills, which recommendation, whether it waits for
approval) against a seeded demo.
"""

import json
import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel
from pydantic_ai import DeferredToolRequests
from pydantic_evals.evaluators import EvaluationReason, Evaluator, EvaluatorContext
from pydantic_evals.online import OnlineEvalConfig
from pydantic_evals.online_capability import OnlineEvaluation

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE = re.compile(r"(?<![\w-])(?:\+|0)\d[\d ()-]{8,}\d(?![\w-])")
REASON_FIELDS = ("reason", "explanation", "why", "recommendation", "reason_for_author")
MIN_REASON = 8


def as_data(output: Any) -> Any:
    """An agent's output as plain data, whichever way the evaluator was reached."""
    if isinstance(output, BaseModel):
        return output.model_dump(mode="json")
    if isinstance(output, DeferredToolRequests):
        return {
            "pending": [{"tool": c.tool_name, "args": c.args_as_dict()} for c in output.approvals]
        }
    return output


@dataclass
class NoContactDetails(Evaluator):
    """The assistants know people by display name only; an address in an output is a leak."""

    def evaluate(self, ctx: EvaluatorContext) -> EvaluationReason:
        text = json.dumps(as_data(ctx.output), default=str)
        found = EMAIL.search(text) or PHONE.search(text)
        return EvaluationReason(
            value=found is None, reason="contact detail in the output" if found else None
        )


def _reasons(node: Any) -> list[str]:
    if isinstance(node, dict):
        own = [v for k, v in node.items() if k in REASON_FIELDS and isinstance(v, str)]
        return own + [r for v in node.values() for r in _reasons(v)]
    if isinstance(node, list):
        return [r for item in node for r in _reasons(item)]
    return []


@dataclass
class ReasonsGiven(Evaluator):
    """Every suggestion says why. An unexplained suggestion cannot be checked by the person
    who has to act on it."""

    def evaluate(self, ctx: EvaluatorContext) -> EvaluationReason:
        short = [r for r in _reasons(as_data(ctx.output)) if len(r.strip()) < MIN_REASON]
        return EvaluationReason(
            value=not short, reason=f"{len(short)} reason(s) too short to check" if short else None
        )


def online(assistant: str) -> OnlineEvaluation:
    """The capability that runs the cheap checks on every live run of one assistant."""
    return OnlineEvaluation(
        evaluators=[NoContactDetails(), ReasonsGiven()],
        config=OnlineEvalConfig(metadata={"assistant": assistant}),
    )


# ---------------------------------------------------------------- offline expectations
def _skills(run: dict) -> dict[str, str]:
    """skill_id -> state the run proposed, from its advice or from a pending save_profile."""
    proposed = {s["skill_id"]: s["state"] for s in (run.get("output") or {}).get("skills", [])}
    for action in run.get("pending", []):
        if action["tool"] == "save_profile":
            proposed |= action["args"].get("skills", {})
    return proposed


def _proposal(run: dict) -> dict:
    for action in run.get("pending", []):
        if action["tool"] == "save_draft":
            return action["args"]
    return run.get("output") or {}


@dataclass
class Expectations(Evaluator):
    """Checks a run against its case's metadata. Each key present becomes one named result:

    * `offers`: skill IDs that must be proposed as offered;
    * `untouched`: skill IDs the run must not propose anything for (the volunteer's exclusions);
    * `approval`: the tool the run must stop and ask approval for (or None: it must not);
    * `cause`, `required`: the project proposal's cause and skills it must require;
    * `recommends`: volunteer IDs the shortlist must include; `only`: the IDs it may include;
    * `recommendation`: the review's recommendation;
    * `cites`: check IDs the audit report must cite; `overall`: its overall verdict;
    * `explains`: project IDs the match explanation must cover.
    """

    def evaluate(self, ctx: EvaluatorContext) -> dict[str, bool]:
        want, run = ctx.metadata or {}, ctx.output
        output = run.get("output") or {}
        results: dict[str, bool] = {}
        if "offers" in want:
            proposed = _skills(run)
            results["offers_expected_skills"] = all(
                proposed.get(s) == "offers" for s in want["offers"]
            )
        if "untouched" in want:
            results["leaves_exclusions_alone"] = not set(want["untouched"]) & set(_skills(run))
        if "approval" in want:
            tools = [a["tool"] for a in run.get("pending", [])]
            results["asks_approval_when_it_should"] = (
                tools == [want["approval"]] if want["approval"] else not tools
            )
        if "cause" in want:
            results["right_cause"] = _proposal(run).get("cause_id") == want["cause"]
        if "required" in want:
            required = set(_proposal(run).get("required_skills", []))
            results["requires_expected_skills"] = set(want["required"]) <= required
        chosen = [r["volunteer_id"] for r in output.get("recommendations", [])]
        if "recommends" in want:
            results["recommends_expected"] = set(want["recommends"]) <= set(chosen)
        if "only" in want:
            results["recommends_only_candidates"] = set(chosen) <= set(want["only"])
        if "recommendation" in want:
            results["right_recommendation"] = output.get("recommendation") == want["recommendation"]
        if "cites" in want:
            cited = {f.get("check_id") for f in output.get("findings", [])}
            results["cites_every_check"] = set(want["cites"]) <= cited
        if "overall" in want:
            results["right_overall"] = output.get("overall") == want["overall"]
        if "explains" in want:
            covered = {m["project_id"] for m in output.get("matches", [])}
            results["explains_expected_matches"] = set(want["explains"]) <= covered
        return results
