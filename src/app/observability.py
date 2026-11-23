"""Pydantic Logfire: traces, metrics and evaluation results for the whole application.

`configure` is called once per process (the web app's factory and the CLI both call it). With no
`LOGFIRE_TOKEN` everything below still runs and is simply sent nowhere, so the demo needs no
account and the tests can read the spans in memory.

What is instrumented:

* every Pydantic AI agent run, model request and tool call (`instrument_pydantic_ai`);
* every HTTP request (`instrument_app`), except health checks and static files;
* the projector's drain and the placement sweep, as spans (`span`);
* counters for audit events and assistant runs, and a histogram of tokens per run.

Privacy: Logfire's default scrubbing redacts passwords, tokens, cookies and similar; request
headers, bodies and endpoint arguments are not captured; and prompts, tool arguments and model
output are left off the agent spans in production unless LOGFIRE_INCLUDE_CONTENT says otherwise.
"""

import threading
from contextlib import AbstractContextManager
from importlib.metadata import PackageNotFoundError, version

import logfire
from fastapi import FastAPI

from app.config import Settings

SERVICE = "volunteer-network"
EXCLUDED_URLS = "/healthz,/static/.*,/brand/.*,/org-assets/.*,/theme.css"


def _version() -> str:
    try:
        return version("hackentry")
    except PackageNotFoundError:  # a checkout that was never installed
        return "0"


_lock = threading.Lock()
_configured = False

_audit_events = logfire.metric_counter(
    "audit.events", unit="1", description="Audit events written, by action family and outcome"
)
_agent_runs = logfire.metric_counter(
    "agent.runs", unit="1", description="Assistant runs finished, by agent and status"
)
_agent_tokens = logfire.metric_histogram(
    "agent.tokens", unit="token", description="Tokens used by one assistant run"
)


def configure(settings: Settings, *, force: bool = False) -> None:
    """Set Logfire up for this process. Later calls do nothing (tests pass `force`)."""
    global _configured
    with _lock:
        if _configured and not force:
            return
        token = settings.logfire_token.get_secret_value()
        logfire.configure(
            send_to_logfire="if-token-present",
            token=token or None,
            service_name=SERVICE,
            service_version=_version(),
            environment=settings.app_env,
            console=False,
            inspect_arguments=False,
        )
        logfire.instrument_pydantic_ai(include_content=settings.include_agent_content)
        _configured = True


def mark_configured() -> None:
    """Tell `configure` that something else (a test's capture fixture) owns the configuration."""
    global _configured
    _configured = True


def _no_arguments(_request, _attributes) -> None:
    """Logfire would otherwise attach each endpoint's parsed arguments to the span: the sign-in
    email, a profile, what a volunteer typed to an assistant. The route and status are enough."""
    return None


def instrument_app(app: FastAPI) -> None:
    logfire.instrument_fastapi(
        app,
        excluded_urls=EXCLUDED_URLS,
        capture_headers=False,
        request_attributes_mapper=_no_arguments,
    )


def span(name: str, **attributes) -> AbstractContextManager:
    """A span around a unit of background work. Attributes must not be personal data."""
    return logfire.span(name, **attributes)


def current_trace_id() -> str:
    """The trace the caller is in, as Logfire shows it, or '' outside one."""
    from opentelemetry import trace

    context = trace.get_current_span().get_span_context()
    return f"{context.trace_id:032x}" if context.is_valid else ""


def count_audit(action: str, outcome: str) -> None:
    _audit_events.add(1, {"action": action.split(".", 1)[0], "outcome": outcome})


def count_agent_run(agent: str, status: str, tokens: int) -> None:
    _agent_runs.add(1, {"agent": agent, "status": status})
    if tokens:
        _agent_tokens.record(tokens, {"agent": agent})
