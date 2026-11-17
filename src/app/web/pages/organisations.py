"""Home, the organisation directory, joining and leaving, and an organisation's volunteers."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app import auth, matching, services
from app.catalogue import get_catalogue
from app.org_profiles import profile_for
from app.web.deps import RT, Org, User, UserForm, drain, nav_for, redirect, render, safe_next

router = APIRouter(include_in_schema=False, default_response_class=HTMLResponse)


@router.get("/home")
def home(request: Request, rt: RT, session: User):
    volunteering = [o["id"] for o in nav_for(request, session).volunteering]
    return render(
        request, "home.html", session,
        matches=matching.volunteer_matches(rt.db, rt.graph, session.user_id, volunteering),
        queues=services.review_counts(rt.db, session.user_id),
        submissions=services.my_submissions(rt.db, session.user_id)[:5],
    )  # fmt: skip


@router.get("/orgs")
def orgs_page(request: Request, rt: RT, session: User):
    directory = services.directory(rt.db, session.user_id)
    return render(
        request, "orgs.html", session,
        directory=[{**o, "profile": profile_for(o)} for o in directory],
    )  # fmt: skip


@router.get("/showcase")
def showcase(request: Request, rt: RT, session: User):
    """Every organisation's story and the published projects it wants volunteers for."""
    cat = get_catalogue()
    featured = [
        {
            **o,
            "profile": profile_for(o),
            "projects": [
                {
                    **p,
                    "cause_name": cat.cause_name(p["cause_id"]),
                    "required_names": [cat.skill_name(s) for s in p["required"]],
                    "optional_names": [cat.skill_name(s) for s in p["optional"]],
                }
                for p in o["projects"]
            ],
        }
        for o in services.showcase(rt.db, session.user_id)
    ]
    return render(request, "showcase.html", session, featured=featured)


@router.post("/orgs/switch")
def switch(rt: RT, session: User, form: UserForm):
    org_id = str(form.get("org_id", ""))
    services.require_access(rt.db, session.user_id, org_id)  # switching never grants
    auth.set_active_org(rt.db, session.token_hash, org_id)
    return redirect(safe_next(str(form.get("next", "")), f"/o/{org_id}/projects"), "switched")


@router.post("/orgs/{org_id}/join")
def join(org_id: str, rt: RT, session: User, _form: UserForm):
    services.set_membership(rt.db, session.user_id, org_id, opted_in=True)
    if session.active_org_id is None:
        auth.set_active_org(rt.db, session.token_hash, org_id)
    drain(rt)
    return redirect("/orgs", "joined")


@router.post("/orgs/{org_id}/leave")
def leave(org_id: str, rt: RT, session: User, _form: UserForm):
    services.set_membership(rt.db, session.user_id, org_id, opted_in=False)
    drain(rt)
    return redirect("/orgs", "left")


# ---------------------------------------------------------------- org admin: volunteers
@router.get("/o/{org_id}/volunteers")
def org_volunteers(org: Org, request: Request, rt: RT, session: User):
    people = services.org_volunteers(rt.db, session.user_id, org["id"])
    return render(request, "org_volunteers.html", session, org=org, people=people)


@router.post("/o/{org_id}/volunteers/{user_id}/remove")
def org_remove_volunteer(org_id: str, user_id: str, rt: RT, session: User, form: UserForm):
    services.remove_volunteer(
        rt.db, session.user_id, org_id, user_id, reason=str(form.get("reason", ""))
    )
    drain(rt)
    return redirect(f"/o/{org_id}/volunteers", "removed")
