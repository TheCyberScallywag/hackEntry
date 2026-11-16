"""Web app: FastAPI + Jinja2, server-rendered pages and plain forms (no client JavaScript needed).

Every protected route resolves the user from the opaque session cookie and checks access against
the organisation named in the URL or form, so switching the active organisation is only a
convenience and never a grant. Every POST carries a CSRF token.

This module builds the app. The pages live in app.web.pages, what they share in app.web.deps,
error responses in app.web.errors and the JSON API in app.web.api.
"""

import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from app.config import BRAND_DIR, Settings, get_settings
from app.runtime import Runtime, bootstrap, build_runtime
from app.web import api, pages
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


def _projector_loop(rt: Runtime, stop: threading.Event) -> None:
    while not stop.wait(rt.settings.projector_interval_seconds):
        try:
            rt.projector.drain()
        except Exception:  # keep the loop alive; errors are recorded per item
            log.exception("background projection failed")


def create_app(settings: Settings | None = None, *, background: bool = True) -> FastAPI:
    settings = settings or get_settings()

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
        title="Data Volunteer Network API",
        description=(api.__doc__ or "").strip(),
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.mount("/brand", StaticFiles(directory=BRAND_DIR / "images"), name="brand")
    app.include_router(api.router)
    for router in pages.routers:
        app.include_router(router)

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
        if path != "/healthz" and not path.startswith("/static"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    register_errors(app)
    return app
