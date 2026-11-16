"""Project suggestions: the author's side, and review and withdrawal by the organisation."""

from collections.abc import Callable

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from app import auth, services
from app.web.deps import (
    RT,
    Org,
    User,
    UserForm,
    draft_from,
    drain,
    redirect,
    render,
    to_int,
)

router = APIRouter(include_in_schema=False, default_response_class=HTMLResponse)


def _act(
    request: Request, rt: RT, session: auth.Session, sid: str, msg: str, action: Callable[[], None]
) -> Response:
    """Run a change to one suggestion, then go back to it. Invalid input re-renders the
    suggestion page with the message; other errors go to the shared error handlers."""
    try:
        action()
    except services.Invalid as error:
        sub = services.get_submission(rt.db, session.user_id, sid)
        return render(request, "submission.html", session, sub=sub, error=str(error),
                      status_code=400)  # fmt: skip
    drain(rt)
    return redirect(f"/submissions/{sid}", msg)


# ---------------------------------------------------------------- the author
@router.get("/submissions")
def my_submissions(request: Request, rt: RT, session: User):
    return render(
        request, "submissions.html", session,
        submissions=services.my_submissions(rt.db, session.user_id),
    )  # fmt: skip


@router.get("/submissions/new")
def new_submission(request: Request, session: User):
    return render(request, "submission_form.html", session, draft=None,
                  target=request.query_params.get("org", ""))  # fmt: skip


@router.post("/submissions/new")
def create_submission(request: Request, rt: RT, session: User, form: UserForm):
    draft = draft_from(form)
    target = str(form.get("org_id", ""))
    try:
        sid = services.create_submission(rt.db, session.user_id, target, draft)
    except services.Invalid as error:
        return render(request, "submission_form.html", session, draft=draft, target=target,
                      error=str(error), status_code=400)  # fmt: skip
    return redirect(f"/submissions/{sid}", "submitted")


@router.get("/submissions/{sid}")
def submission(sid: str, request: Request, rt: RT, session: User):
    sub = services.get_submission(rt.db, session.user_id, sid)
    return render(request, "submission.html", session, sub=sub)


@router.post("/submissions/{sid}/revise")
def revise(sid: str, request: Request, rt: RT, session: User, form: UserForm):
    return _act(
        request, rt, session, sid, "revised",
        lambda: services.revise_submission(
            rt.db, session.user_id, sid, draft_from(form),
            expected_version=to_int(form.get("version")),
        ),
    )  # fmt: skip


# ---------------------------------------------------------------- review
@router.get("/o/{org_id}/review")
def review_queue(org: Org, request: Request, rt: RT, session: User):
    queue = services.review_queue(rt.db, session.user_id, org["id"])
    return render(request, "review_queue.html", session, org=org, queue=queue)


@router.post("/o/{org_id}/review/{sid}")
def review(org_id: str, sid: str, request: Request, rt: RT, session: User, form: UserForm):
    return _act(
        request, rt, session, sid, "reviewed",
        lambda: services.review_submission(
            rt.db, session.user_id, org_id, sid,
            action=str(form.get("action", "")),
            reason=str(form.get("reason", "")),
            private_note=str(form.get("private_note", "")),
            revision=to_int(form.get("revision")),
            expected_version=to_int(form.get("version")),
        ),
    )  # fmt: skip


@router.post("/o/{org_id}/projects/{sid}/withdraw")
def withdraw(org_id: str, sid: str, request: Request, rt: RT, session: User, form: UserForm):
    return _act(
        request, rt, session, sid, "withdrawn",
        lambda: services.withdraw_project(
            rt.db, session.user_id, org_id, sid,
            reason=str(form.get("reason", "")), expected_version=to_int(form.get("version")),
        ),
    )  # fmt: skip
