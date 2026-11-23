"""Runs an assistant for a signed-in user, and keeps the record of every run.

One code path for every agent, so each run is:

* **authorised first** (`Assistant.authorise`), before a token is spent;
* **limited**: a per-account daily quota, and per-run caps on requests, tool calls, tokens and
  wall-clock time (`UsageLimits`), so a loop cannot run up a bill;
* **recorded** in `agent_runs` (who, which agent, cost, tools called, how it ended) and in the
  audit trail, and traced in Logfire under one span whose trace ID is kept on the row;
* **resumable**: when a tool needs approval the run stops, its conversation is stored, and
  `resume` continues it with the user's decision. The conversation is deleted once it ends.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel
from pydantic_ai import (
    Agent,
    AgentRunError,
    DeferredToolRequests,
    DeferredToolResults,
    ModelMessagesTypeAdapter,
    ToolDenied,
    UsageLimits,
)
from pydantic_ai.run import AgentRunResult

from app import audit, observability, services
from app.agents.deps import AgentDeps
from app.runtime import Runtime
from app.sqlstore import days_ago, new_id, now


class AgentsDisabled(RuntimeError):
    """No model is configured (AGENT_MODEL), so the assistants are switched off."""


class QuotaExceeded(RuntimeError):
    """The account has used its assistant runs for the day."""


class AgentFailed(RuntimeError):
    """The run could not finish: the model failed, or a limit was reached. Safe to show."""


Execute = Callable[..., AgentRunResult[Any]]


@dataclass(frozen=True)
class Assistant:
    """An agent as the application offers it."""

    name: str
    title: str
    audience: str  # who may use it, in words, for the listing
    description: str
    agent: Agent[AgentDeps, Any]
    # Raises a services error when this user may not run it for this subject.
    authorise: Callable[[AgentDeps], None] = lambda deps: None
    # How to run it. The default is one agent run; the audit assistant runs a graph instead.
    execute: Execute | None = None


@dataclass
class Run:
    """What a caller gets back: the outcome of a run, or the approval it is waiting for."""

    run_id: str
    agent: str
    status: str  # completed | awaiting_approval
    output: dict | None = None
    pending: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)


REGISTRY: dict[str, Assistant] = {}


def register(assistant: Assistant) -> Assistant:
    REGISTRY[assistant.name] = assistant
    return assistant


def _limits(rt: Runtime) -> UsageLimits:
    s = rt.settings
    return UsageLimits(
        request_limit=s.agent_request_limit,
        tool_calls_limit=s.agent_tool_calls_limit,
        total_tokens_limit=s.agent_total_tokens_limit,
    )


def _check_quota(rt: Runtime, actor_id: str) -> None:
    limit = rt.settings.agent_daily_runs
    if not limit:
        return
    with rt.db.read() as conn:
        used = conn.execute(
            "SELECT count(*) FROM agent_runs WHERE actor_id = ? AND created_at >= ?",
            (actor_id, days_ago(1)),
        ).fetchone()[0]
    if used >= limit:
        audit.record_now(
            rt.db, "agent.run", actor_id=actor_id, outcome="denied", source="agent",
            detail={"reason": "daily quota"},
        )  # fmt: skip
        raise QuotaExceeded(f"You have used today's {limit} assistant runs. Try again tomorrow.")


def start(
    rt: Runtime,
    name: str,
    *,
    actor_id: str,
    prompt: str,
    org_id: str | None = None,
    subject_id: str = "",
    days: int = 7,
) -> Run:
    """Run an assistant for `actor_id`. Raises a services error if they may not."""
    assistant = REGISTRY[name]
    if rt.agent_model is None:
        raise AgentsDisabled("The assistants are not configured on this server.")
    deps = AgentDeps(
        rt, actor_id, run_id=new_id("run"), org_id=org_id, subject_id=subject_id, days=days
    )
    try:
        assistant.authorise(deps)
    except services.Forbidden, services.NotFound:
        audit.record_now(
            rt.db, "agent.run", actor_id=actor_id, outcome="denied", org_id=org_id,
            target_kind="agent", target_id=name, source="agent",
        )  # fmt: skip
        raise
    _check_quota(rt, actor_id)
    with rt.db.tx() as conn:
        conn.execute(
            "INSERT INTO agent_runs (id, agent, actor_id, org_id, subject_id, status, model, "
            "created_at) VALUES (?, ?, ?, ?, ?, 'running', ?, ?)",
            # deps, not the arguments: `authorise` may have filled in the organisation
            (deps.run_id, name, actor_id, deps.org_id, subject_id, _model_name(rt), now()),
        )
    return _execute(rt, assistant, deps, prompt=prompt)


def resume(rt: Runtime, *, actor_id: str, run_id: str, approve: bool, reason: str = "") -> Run:
    """Continue a run that is waiting for approval, with the user's answer for every pending
    action. Only the person who started the run can answer."""
    if rt.agent_model is None:
        raise AgentsDisabled("The assistants are not configured on this server.")
    with rt.db.tx() as conn:
        row = conn.execute(
            "SELECT agent, org_id, subject_id, pending, messages FROM agent_runs "
            "WHERE id = ? AND actor_id = ?",
            (run_id, actor_id),
        ).fetchone()
        if row is None:
            raise services.NotFound("run")
        # Claimed in this transaction, so a double click cannot run the approved action twice.
        claimed = conn.execute(
            "UPDATE agent_runs SET status = 'running', messages = NULL, pending = '[]' "
            "WHERE id = ? AND status = 'awaiting_approval'",
            (run_id,),
        ).rowcount
        if not claimed:
            raise services.Conflict("This run is not waiting for approval any more.")
        audit.record(
            conn, "agent.approval", actor_id=actor_id, outcome="ok", org_id=row["org_id"],
            target_kind="run", target_id=run_id, source="agent",
            detail={"approved": approve, "tools": [p["tool"] for p in json.loads(row["pending"])]},
        )  # fmt: skip
    assistant = REGISTRY[row["agent"]]
    deps = AgentDeps(
        rt, actor_id, run_id=run_id, org_id=row["org_id"], subject_id=row["subject_id"]
    )
    denied = ToolDenied(reason.strip()[: services.REASON_MAX] or "The user said no.")
    answers = DeferredToolResults(
        approvals={
            p["tool_call_id"]: True if approve else denied for p in json.loads(row["pending"])
        }
    )
    history = ModelMessagesTypeAdapter.validate_json(row["messages"])
    return _execute(rt, assistant, deps, history=history, answers=answers)


def _model_name(rt: Runtime) -> str:
    return getattr(rt.agent_model, "model_name", "") or ""


def _execute(
    rt: Runtime,
    assistant: Assistant,
    deps: AgentDeps,
    *,
    prompt: str | None = None,
    history: list | None = None,
    answers: DeferredToolResults | None = None,
) -> Run:
    execute = assistant.execute or assistant.agent.run_sync
    with observability.span(
        "assistant {agent}", agent=assistant.name, run_id=deps.run_id, actor_id=deps.actor_id,
        org_id=deps.org_id or "", resumed=history is not None,
    ):  # fmt: skip
        trace_id = observability.current_trace_id()
        try:
            result = execute(
                prompt,
                deps=deps,
                model=rt.agent_model,
                usage_limits=_limits(rt),
                message_history=history,
                deferred_tool_results=answers,
                timeout=rt.settings.agent_timeout_seconds,
                metadata={"run_id": deps.run_id, "assistant": assistant.name},
            )
        except AgentRunError as error:  # the model failed, misbehaved, or a limit was reached
            _finish(rt, assistant, deps, "failed", trace_id, error=type(error).__name__)
            raise AgentFailed(
                "The assistant could not finish. Nothing was changed; try again shortly."
            ) from error
        except Exception as error:
            _finish(rt, assistant, deps, "failed", trace_id, error=type(error).__name__)
            raise
        usage = result.usage
        output = result.output
        if isinstance(output, DeferredToolRequests):
            pending = [
                {
                    "tool_call_id": call.tool_call_id,
                    "tool": call.tool_name,
                    "args": call.args_as_dict(),
                    "summary": (output.metadata.get(call.tool_call_id) or {}).get("summary", ""),
                }
                for call in output.approvals
            ]
            _finish(
                rt, assistant, deps, "awaiting_approval", trace_id, usage=usage, pending=pending,
                messages=result.all_messages_json().decode(),
            )  # fmt: skip
            return Run(deps.run_id, assistant.name, "awaiting_approval", None, pending,
                       _usage_dict(usage))  # fmt: skip
        data = output.model_dump(mode="json") if isinstance(output, BaseModel) else output
        _finish(rt, assistant, deps, "completed", trace_id, usage=usage, output=data)
        return Run(deps.run_id, assistant.name, "completed", data, [], _usage_dict(usage))


def _usage_dict(usage) -> dict:
    return {
        "requests": usage.requests,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "tool_calls": usage.tool_calls,
    }


def _finish(
    rt: Runtime,
    assistant: Assistant,
    deps: AgentDeps,
    status: str,
    trace_id: str,
    *,
    usage=None,
    output=None,
    pending: list[dict] | None = None,
    messages: str | None = None,
    error: str = "",
) -> None:
    """Close (or park) the run's row. Usage adds up across the parts of a resumed run."""
    used = _usage_dict(usage) if usage else dict.fromkeys(
        ("requests", "input_tokens", "output_tokens", "tool_calls"), 0
    )  # fmt: skip
    with rt.db.tx() as conn:
        before = json.loads(
            conn.execute("SELECT tools FROM agent_runs WHERE id = ?", (deps.run_id,)).fetchone()[0]
        )
        conn.execute(
            "UPDATE agent_runs SET status = ?, requests = requests + ?, "
            "input_tokens = input_tokens + ?, output_tokens = output_tokens + ?, "
            "tool_calls = tool_calls + ?, tools = ?, error = ?, output = ?, pending = ?, "
            "messages = ?, trace_id = ?, finished_at = ? WHERE id = ?",
            (status, used["requests"], used["input_tokens"], used["output_tokens"],
             used["tool_calls"], json.dumps(before + deps.tools_used), error,
             json.dumps(output) if output is not None else None, json.dumps(pending or []),
             messages, trace_id, None if status == "awaiting_approval" else now(), deps.run_id),
        )  # fmt: skip
        audit.record(
            conn, "agent.run", actor_id=deps.actor_id,
            outcome="error" if status == "failed" else "ok", org_id=deps.org_id,
            target_kind="agent", target_id=assistant.name, source="agent",
            detail={"run_id": deps.run_id, "status": status, "tools": deps.tools_used,
                    **({"error": error} if error else {})},
        )  # fmt: skip
    observability.count_agent_run(
        assistant.name, status, used["input_tokens"] + used["output_tokens"]
    )


# ---------------------------------------------------------------- reading the record
_RUN_COLUMNS = (
    "id, agent, org_id, subject_id, status, model, requests, input_tokens, output_tokens, "
    "tool_calls, tools, error, output, pending, trace_id, created_at, finished_at"
)


def _shape(row) -> dict:
    data = dict(row)
    for key in ("tools", "pending"):
        data[key] = json.loads(data[key])
    data["output"] = json.loads(data["output"]) if data["output"] else None
    return data


def my_runs(rt: Runtime, actor_id: str, *, limit: int = 20) -> list[dict]:
    """The caller's own runs, newest first."""
    with rt.db.read() as conn:
        rows = conn.execute(
            f"SELECT {_RUN_COLUMNS} FROM agent_runs WHERE actor_id = ? "
            "ORDER BY created_at DESC, id LIMIT ?",
            (actor_id, max(1, min(limit, 100))),
        ).fetchall()
    return [_shape(r) for r in rows]


def get_run(rt: Runtime, actor_id: str, run_id: str) -> dict:
    with rt.db.read() as conn:
        row = conn.execute(
            f"SELECT {_RUN_COLUMNS} FROM agent_runs WHERE id = ? AND actor_id = ?",
            (run_id, actor_id),
        ).fetchone()
    if row is None:
        raise services.NotFound("run")
    return _shape(row)
