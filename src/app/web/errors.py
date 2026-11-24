"""One place that turns domain errors into responses: JSON under /api/, an error page elsewhere.

The handlers are plain functions, so Starlette runs them in its thread pool: rendering the error
page reads SQLite for the page frame.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import agents, services
from app.graph.store import GraphBusy, GraphError
from app.web.deps import CsrfFailed, LoginRequired, optional_session, redirect, render

log = logging.getLogger(__name__)

FORBIDDEN_DETAIL = {
    "reviewer": "Only this organisation's reviewers and admins can do that.",
    "team_coordinator": "Only this organisation's team coordinators and admins can do that.",
    "org_admin": "Only this organisation's admins can do that.",
    "volunteer": "Join this organisation as a volunteer first.",
    "any": "You are not a member of this organisation.",
    "active": "Your account must be vetted and active first.",
    "moderator": "Only moderators and system admins can do that.",
    "admin": "Only system admins can do that.",
}


def _graph_detail(exc: Exception) -> str:
    log.warning("graph error: %s", exc)
    if isinstance(exc, GraphBusy):
        return "The matching graph is busy (another process holds the store). Try again shortly."
    return "The matching graph could not answer. Try again shortly."


@dataclass(frozen=True)
class _Rule:
    status: int  # for the error page
    title: str
    detail: Callable[[Exception], str]  # for the error page
    api_status: int | None = None  # defaults to `status`
    api_detail: Callable[[Exception], str] | None = None  # defaults to `detail`


_RULES: dict[type[Exception], _Rule] = {
    services.Invalid: _Rule(400, "Check your input", str, api_status=422),
    CsrfFailed: _Rule(403, "Form expired", lambda _: "Reload the page and submit the form again."),
    services.NotFound: _Rule(
        404,
        "Not found",
        lambda _: "That page does not exist or is not visible to you.",
        api_detail=lambda _: "Not found",
    ),
    services.Forbidden: _Rule(
        403, "Not allowed", lambda exc: FORBIDDEN_DETAIL.get(str(exc), str(exc))
    ),
    services.Conflict: _Rule(409, "Already changed", str),
    GraphError: _Rule(503, "Matching unavailable", _graph_detail),
    agents.AgentsDisabled: _Rule(503, "Assistants unavailable", str),
    agents.QuotaExceeded: _Rule(429, "Daily limit reached", str),
    agents.AgentFailed: _Rule(502, "The assistant could not finish", str),
}


def _handler(rule: _Rule):
    def handle(request: Request, exc: Exception):
        if request.url.path.startswith("/api/"):
            detail = (rule.api_detail or rule.detail)(exc)
            return JSONResponse({"detail": detail}, status_code=rule.api_status or rule.status)
        return render(
            request, "error.html", optional_session(request), rule.status,
            title=rule.title, detail=rule.detail(exc),
        )  # fmt: skip

    return handle


def register_errors(app: FastAPI) -> None:
    for exc_type, rule in _RULES.items():
        app.add_exception_handler(exc_type, _handler(rule))

    @app.exception_handler(LoginRequired)
    def _login(request: Request, _exc: LoginRequired):
        return redirect("/signin?" + urlencode({"next": request.url.path}))
