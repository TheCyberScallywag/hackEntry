"""Signing up, in and out; the user's own profile, vetting and notifications."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app import audit, auth, notify, profile_view, services
from app.catalogue import get_catalogue
from app.web.deps import (
    RT,
    SESSION_COOKIE,
    AnonForm,
    MaybeUser,
    User,
    UserForm,
    drain,
    redirect,
    render,
    require_signup,
    safe_next,
    set_session_cookie,
    to_int,
)

router = APIRouter(include_in_schema=False, default_response_class=HTMLResponse)


@router.get("/")
def index(request: Request, session: MaybeUser):
    if session:
        return redirect("/home")
    return render(request, "index.html")


# ---------------------------------------------------------------- identity
def _start_session(request: Request, rt: RT, user_id: str, target: str, msg: str):
    response = redirect(target, msg)
    token = auth.create_session(
        rt.db, user_id, kind="cookie", ttl_hours=rt.settings.session_ttl_hours
    )
    set_session_cookie(request, response, token)
    return response


@router.get("/signup", dependencies=[Depends(require_signup)])
def signup_form(request: Request):
    return render(request, "signup.html")


@router.post("/signup", dependencies=[Depends(require_signup)])
def signup(request: Request, rt: RT, form: AnonForm):
    # Only these three fields are read: a posted "role" or "org" is ignored by design.
    email, name = str(form.get("email", "")), str(form.get("display_name", ""))
    try:
        user_id = auth.signup(
            rt.db, email=email, display_name=name, password=str(form.get("password", ""))
        )
    except auth.AuthError as error:
        return render(request, "signup.html", error=str(error), email=email, display_name=name,
                      status_code=400)  # fmt: skip
    return _start_session(request, rt, user_id, "/orgs", "welcome")


@router.get("/signin")
def signin_form(request: Request):
    return render(request, "signin.html", next=request.query_params.get("next", ""))


@router.post("/signin")
def signin(request: Request, rt: RT, form: AnonForm):
    email = str(form.get("email", ""))
    user_id = auth.authenticate(rt.db, email=email, password=str(form.get("password", "")))
    nxt = str(form.get("next", ""))
    audit.record_now(
        rt.db, "auth.sign_in", actor_id=user_id, outcome="ok" if user_id else "denied",
        source="web",
    )  # fmt: skip
    if user_id is None:
        return render(request, "signin.html", error="Wrong email or password.", email=email,
                      next=nxt, status_code=400)  # fmt: skip
    if old := request.cookies.get(SESSION_COOKIE):
        auth.revoke_session(rt.db, auth.token_hash(old))  # no session fixation
    return _start_session(request, rt, user_id, safe_next(nxt, "/home"), "signed_in")


@router.post("/signout")
def signout(rt: RT, session: User, _form: UserForm):
    auth.revoke_session(rt.db, session.token_hash)
    response = redirect("/", "signed_out")
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


# ---------------------------------------------------------------- profile + onboarding
@router.get("/profile")
def profile_page(request: Request, rt: RT, session: User):
    return render(
        request, "profile.html", session,
        view=profile_view.overview(rt.db, rt.graph, session.user_id),
        history=services.engagements.my_history(rt.db, session.user_id),
    )  # fmt: skip


@router.post("/profile")
def profile_save(request: Request, rt: RT, session: User, form: UserForm):
    cat = get_catalogue()
    skills = {s.id: str(form.get(f"skill:{s.id}", "none")) for s in cat.skills}
    causes = {c.id: str(form.get(f"cause:{c.id}", "neutral")) for c in cat.causes}
    # A form without the level fields (an older page, a script) leaves the levels alone.
    levels = None
    if any(key.startswith("level:") for key in form):
        levels = {
            s.id: to_int(form.get(f"level:{s.id}")) for s in cat.skills if form.get(f"level:{s.id}")
        }
    try:
        services.update_profile(
            rt.db, session.user_id,
            display_name=str(form.get("display_name", "")),
            skills=skills, causes=causes, levels=levels,
            expected_version=to_int(form.get("version")),
        )  # fmt: skip
    except services.Invalid as error:
        view = profile_view.overview(rt.db, rt.graph, session.user_id)
        history = services.engagements.my_history(rt.db, session.user_id)
        return render(request, "profile.html", session, view=view, history=history,
                      error=str(error), status_code=400)  # fmt: skip
    drain(rt)
    return redirect("/profile", "profile_saved")


@router.post("/me/history-sharing")
def history_sharing(rt: RT, session: User, form: UserForm):
    """One switch per organisation I volunteer with: may it see a summary of my work elsewhere?"""
    history = services.engagements.my_history(rt.db, session.user_id)
    for org in history["sharing"]:
        wanted = form.get(f"share:{org['org_id']}") == "1"
        if wanted != org["share_history"]:
            services.engagements.set_share_history(
                rt.db, session.user_id, org["org_id"], share=wanted
            )
    drain(rt)
    return redirect("/profile#history", "sharing_saved")


@router.post("/me/vetting")
def submit_vetting(rt: RT, session: User, _form: UserForm):
    services.submit_for_vetting(rt.db, session.user_id)
    return redirect("/profile", "vetting")


# ---------------------------------------------------------------- notifications
@router.get("/notifications")
def notifications_page(request: Request, rt: RT, session: User):
    items = notify.list_for(rt.db, session.user_id)
    return render(request, "notifications.html", session, items=items)


@router.post("/notifications/read-all")
def notifications_read(rt: RT, session: User, _form: UserForm):
    notify.mark_read(rt.db, session.user_id)
    return redirect("/notifications", "read")
