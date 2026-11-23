"""Web app: FastAPI + Jinja2, server-rendered pages and plain forms (no client JavaScript needed).

Every protected route resolves the user from the opaque session cookie and checks access against
the organisation named in the URL or form, so switching the active organisation is only a
convenience and never a grant. Every POST carries a CSRF token.

This module builds the app. The pages live in app.web.pages, what they share in app.web.deps,
error responses in app.web.errors and the JSON API in app.web.api.
"""

import logging
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from app import audit, mailer, observability
from app.brandkit import tokens_css
from app.config import BRAND_DIR, ORG_ASSETS_DIR, Settings, get_settings
from app.meta import get_meta
from app.runtime import Runtime, bootstrap, build_runtime, chase_invitations
from app.web import agents_api, api, onboarding_api, pages
from app.web.deps import HERE, RT
from app.web.errors import register_errors

log = logging.getLogger(__name__)

PAGE_CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; form-action 'self'; "
    "frame-ancestors 'none'; base-uri 'none'"
)
DOCS_CSP = (  # Swagger UI: its CDN assets and inline bootstrap
    "default-src 'self'; img-src 'self' data: https://fastapi.tiangolo.com; "
    "style-src 'self' https://cdn.jsdelivr.net; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "frame-ancestors 'none'; base-uri 'none'"
)


# Sign-in is audited where it happens (auth.sign_in), with the account it was for.
UNAUDITED_PATHS = ("/signin", "/api/v1/session")
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def _audit_request(rt: Runtime, request: Request, status: int) -> None:
    """One audit event for a request that changed something or was refused. The action names
    the route as declared (`/orgs/{org_id}/membership`), never the IDs in the URL."""
    session = getattr(request.state, "session", None)
    route = request.scope.get("route")
    path = request.url.path
    api_call = path.startswith("/api/")
    audit.record_now(
        rt.db,
        f"http.{request.method.lower()}",
        actor_id=session.user_id if session else None,
        outcome=audit.outcome_for(status),
        target_kind="route",
        target_id=getattr(route, "path", "") or "(no route)",
        org_id=request.path_params.get("org_id") if status < 400 else None,
        source="api" if api_call else "web",
        detail={"status": status},
    )


CHASE_EVERY_SECONDS = 3600  # unanswered invitations are counted in days


def _projector_loop(rt: Runtime, stop: threading.Event) -> None:
    last_chase = time.monotonic()  # bootstrap has just chased
    while not stop.wait(rt.settings.projector_interval_seconds):
        try:
            with observability.span("projector drain") as span:
                result = rt.projector.drain()
                span.set_attributes({"done": result.done, "failed": result.failed})
            mailer.deliver(rt.db, rt.settings)  # queued onboarding emails, to the SMTP sidecar
            if time.monotonic() - last_chase >= CHASE_EVERY_SECONDS:
                last_chase = time.monotonic()
                chase_invitations(rt)
        except Exception:  # keep the loop alive; errors are recorded per item
            log.exception("background projection failed")


def create_app(settings: Settings | None = None, *, background: bool = True) -> FastAPI:
    settings = settings or get_settings()
    observability.configure(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        rt = build_runtime(settings)
        await run_in_threadpool(bootstrap, rt)
        app.state.rt = rt
        stop = threading.Event()
        worker = None
        if background:
            worker = threading.Thread(target=_projector_loop, args=(rt, stop), daemon=True)
            worker.start()
        yield
        stop.set()
        if worker:
            await run_in_threadpool(worker.join, 5)

    app = FastAPI(
        title=f"{get_meta().name} API",
        description=(api.__doc__ or "").strip(),
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.mount("/brand", StaticFiles(directory=BRAND_DIR / "images"), name="brand")
    app.mount("/org-assets", StaticFiles(directory=ORG_ASSETS_DIR), name="org-assets")
    app.include_router(api.router)
    app.include_router(agents_api.router)
    app.include_router(onboarding_api.router)
    for router in pages.routers:
        app.include_router(router)

    @app.get("/theme.css", include_in_schema=False)
    def theme_css() -> Response:
        """The brand's colours and fonts as CSS custom properties (style.css uses nothing else)."""
        return Response(
            tokens_css(get_meta()),
            media_type="text/css",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    @app.get("/healthz", include_in_schema=False)
    def healthz(rt: RT) -> dict:
        status = rt.projector.status(limit=0)
        return {"status": "ok", "pending_projections": status.pending, "failed": status.failed}

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        response.headers.setdefault(
            "Content-Security-Policy", DOCS_CSP if path == "/api/docs" else PAGE_CSP
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if path != "/healthz" and not path.startswith(("/static", "/org-assets")):
            response.headers.setdefault("Cache-Control", "no-store")
        status = response.status_code
        refused = status in (401, 403)
        if (request.method not in SAFE_METHODS or refused) and path not in UNAUDITED_PATHS:
            try:
                await run_in_threadpool(_audit_request, request.app.state.rt, request, status)
            except Exception:  # the response matters more than its audit row
                log.exception("audit event not written")
        return response

    register_errors(app)
    observability.instrument_app(app)
    return app
