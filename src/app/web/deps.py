"""What every page handler shares: the session, the checked form, rendering and redirects.

Page handlers are plain `def` functions, so FastAPI runs them in its thread pool and the blocking
work they do (SQLite, the Relate store, Argon2) never holds the event loop. The only `async`
pieces are the two form dependencies below, which await the request body and nothing else.
"""

import logging
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode, urlparse

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.datastructures import FormData

from app import auth, notify, placements, profile_view, services
from app.catalogue import get_catalogue
from app.meta import get_meta
from app.runtime import Runtime

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
SESSION_COOKIE = "dvn_session"
ANON_CSRF_COOKIE = "dvn_csrf"

MESSAGES = {
    "welcome": "Welcome! Join an organisation to start volunteering.",
    "signed_in": "Signed in.",
    "signed_out": "Signed out.",
    "joined": "You now volunteer with this organisation.",
    "left": "You have opted out. This organisation no longer sees you as a candidate.",
    "switched": "Active organisation switched.",
    "profile_saved": "Profile saved. Matches update once the graph has caught up.",
    "submitted": "Suggestion submitted for review.",
    "draft_saved": "Draft saved. Only you can see it until you submit it for review.",
    "draft_discarded": "Draft discarded.",
    "revised": "Revision submitted for review.",
    "reviewed": "Decision recorded.",
    "withdrawn": "Project withdrawn from browse and matching.",
    "vetting": "Thanks! Your profile is in screening. We'll notify you here when it's reviewed.",
    "answered": "Answer recorded.",
    "invited": "Invitation sent. They join the team once they say yes.",
    "reopened": "Reopened. They are a candidate again if they still match the project.",
    "left_team": "You left the team.",
    "removed": "Volunteer removed.",
    "accepted": "Accepted. They are on the team now.",
    "suspended": "Suspended from the team. Reinstate them when you are ready.",
    "reinstated": "Reinstated. They are back on the team.",
    "lead_set": "Project lead assigned.",
    "flow_saved": "Team onboarding saved.",
    "read": "Notifications marked as read.",
    "step_done": "Step completed.",
}


class LoginRequired(Exception):
    pass


class CsrfFailed(Exception):
    pass


templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals["catalogue"] = get_catalogue
templates.env.globals["SKILL_STATES"] = profile_view.SKILL_STATES
templates.env.globals["CAUSE_STANCES"] = profile_view.CAUSE_STANCES
templates.env.globals["PROJECT_STATES"] = profile_view.PROJECT_STATES
templates.env.filters["skill_name"] = lambda s: get_catalogue().skill_name(s)
templates.env.filters["state_label"] = lambda s: (
    profile_view.PROJECT_STATES | placements.STATE_LABELS
).get(s, s)


def initials(name: str) -> str:
    """Up to two letters for the avatar: the first of the first and of the last word."""
    words = name.split()
    return "".join(w[0] for w in (words[:1] + words[1:][-1:])).upper() or "?"


templates.env.filters["initials"] = initials


# ------------------------------------------------------------------ session
def get_rt(request: Request) -> Runtime:
    return request.app.state.rt


_UNSET = object()


def optional_session(request: Request) -> auth.Session | None:
    """The signed-in session, looked up once per request (error pages reuse it)."""
    session = getattr(request.state, "session", _UNSET)
    if session is _UNSET:
        session = auth.load_session(
            get_rt(request).db, request.cookies.get(SESSION_COOKIE), kind="cookie"
        )
        request.state.session = session
    return session


def require_session(request: Request) -> auth.Session:
    session = optional_session(request)
    if session is None:
        raise LoginRequired
    return session


RT = Annotated[Runtime, Depends(get_rt)]


def require_signup(request: Request) -> None:
    """Guard for the sign-up routes, web and API: 403 while SIGNUP_ENABLED is off."""
    if not get_rt(request).settings.signup_enabled:
        raise services.Forbidden("Creating new accounts is switched off.")


MaybeUser = Annotated[auth.Session | None, Depends(optional_session)]
User = Annotated[auth.Session, Depends(require_session)]


# ------------------------------------------------------------------ forms (CSRF-checked)
async def _checked(request: Request, expected: str | None) -> FormData:
    form = await request.form()
    if not auth.csrf_matches(expected, str(form.get("csrf_token", ""))):
        raise CsrfFailed
    return form


async def anon_form(request: Request) -> FormData:
    """A form posted before sign-in, checked against the anonymous CSRF cookie."""
    return await _checked(request, request.cookies.get(ANON_CSRF_COOKIE))


async def user_form(request: Request, session: User) -> FormData:
    """A signed-in user's form, checked against their session's CSRF token."""
    return await _checked(request, session.csrf_token)


AnonForm = Annotated[FormData, Depends(anon_form)]
UserForm = Annotated[FormData, Depends(user_form)]


def to_int(value: object) -> int:
    try:
        return int(str(value))
    except ValueError:
        return -1  # fails the optimistic-concurrency check rather than raising


def draft_from(form: FormData) -> services.Draft:
    return services.Draft(
        title=str(form.get("title", "")),
        summary=str(form.get("summary", "")),
        cause_id=str(form.get("cause_id", "")),
        required=[str(v) for v in form.getlist("required")],
        optional=[str(v) for v in form.getlist("optional")],
    )


# ------------------------------------------------------------------ organisation in the URL
def org_in_path(org_id: str, rt: RT, _session: User) -> dict:
    """The organisation named in the URL, or NotFound. Access is checked by the service called."""
    with rt.db.read() as conn:
        return dict(services.get_org(conn, org_id))


Org = Annotated[dict, Depends(org_in_path)]


# ------------------------------------------------------------------ navigation shown on every page
@dataclass(frozen=True)
class Nav:
    actor: services.Actor
    orgs: list[dict]  # organisations the user volunteers with or reviews for
    pending: int  # the user's own profile changes not yet in the graph
    unread: int
    onboarding: int  # organisations whose onboarding steps the user has not finished

    @property
    def volunteering(self) -> list[dict]:
        return [o for o in self.orgs if o["opted_in"]]


def nav_for(request: Request, session: auth.Session) -> Nav:
    """Everything the page frame needs about the user, from one connection, once per request."""
    cached = getattr(request.state, "nav", None)
    if cached is None:
        with get_rt(request).db.read() as conn:
            actor = services.load_actor(conn, session.user_id)
            cached = Nav(
                actor=actor,
                orgs=services.accessible(
                    services.load_directory(conn, session.user_id, admin=actor.admin)
                ),
                pending=conn.execute(
                    "SELECT count(*) FROM outbox WHERE done_at IS NULL AND kind = 'volunteer' "
                    "AND entity_id = ?",
                    (session.user_id,),
                ).fetchone()[0],
                unread=notify.count_unread(conn, session.user_id),
                onboarding=services.onboarding.open_count(conn, session.user_id),
            )
        request.state.nav = cached
    return cached


def active_org(session: auth.Session, orgs: list[dict]) -> dict | None:
    ids = {o["id"]: o for o in orgs}
    if session.active_org_id in ids:
        return ids[session.active_org_id]
    return orgs[0] if orgs else None


# ------------------------------------------------------------------ responses
def _secure(request: Request) -> bool:
    settings = get_rt(request).settings
    if settings.cookie_secure is not None:
        return settings.cookie_secure
    if settings.trust_proxy_headers:
        return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    return request.url.scheme == "https"


def _anon_csrf(request: Request) -> str:
    token = request.cookies.get(ANON_CSRF_COOKIE)
    if not token or len(token) > 128:
        token = secrets.token_urlsafe(32)
        request.state.new_anon_csrf = token
    return token


def safe_next(target: str | None, default: str) -> str:
    if not target:
        return default
    normalized = target.replace("\\", "")
    parsed = urlparse(normalized)
    if (
        normalized.startswith("/")
        and not normalized.startswith("//")
        and not parsed.scheme
        and not parsed.netloc
    ):
        return normalized
    return default


def redirect(path: str, msg: str | None = None) -> RedirectResponse:
    safe_path = safe_next(path, "/")
    if msg:
        safe_path += ("&" if "?" in safe_path else "?") + urlencode({"msg": msg})
    return RedirectResponse(safe_path, status_code=303)


def render(
    request: Request,
    name: str,
    session: auth.Session | None = None,
    status_code: int = 200,
    **ctx,
) -> HTMLResponse:
    nav = nav_for(request, session) if session else None
    response = templates.TemplateResponse(
        request,
        name,
        {
            "session": session,
            "orgs": nav.orgs if nav else [],
            "active": active_org(session, nav.orgs) if nav else None,
            "pending_mine": nav.pending if nav else None,
            "actor": nav.actor if nav else None,
            "unread": nav.unread if nav else None,
            "onboarding_open": nav.onboarding if nav else 0,
            "status_labels": services.VOLUNTEER_STATUS_LABELS,
            "csrf": session.csrf_token if session else _anon_csrf(request),
            "message": MESSAGES.get(request.query_params.get("msg", "")),
            "meta": get_meta(),
            "signup_enabled": get_rt(request).settings.signup_enabled,
            "path": request.url.path,
            **ctx,
        },
        status_code=status_code,
    )
    if token := getattr(request.state, "new_anon_csrf", None):
        response.set_cookie(
            ANON_CSRF_COOKIE, token, httponly=True, samesite="strict", secure=_secure(request)
        )
    return response


def set_session_cookie(request: Request, response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=get_rt(request).settings.session_ttl_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=_secure(request),
        path="/",
    )


def drain(rt: Runtime) -> None:
    """Project right away so the response usually shows fresh results; failures stay queued and
    the background loop retries them. Used by the pages and by the JSON API alike."""
    try:
        rt.projector.drain()
    except Exception:
        log.exception("inline projection failed; the background loop will retry")
