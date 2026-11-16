"""The server-rendered pages: handlers stay off the event loop, placement and volunteer routes,
the re-render of a form on invalid input, and the shared error pages."""

import inspect
import logging

from app import sqlstore
from app.graph.store import GraphBusy
from app.runtime import DEMO_PASSWORD
from app.web import api, pages
from tests.conftest import AUDIT, CS, DK, FOOD_BANK, USERS, WINTER, Web
from tests.test_rbac_onboarding import placement
from tests.test_submissions import DRAFT, _review, _state


def _membership(web: Web, user: str, org: str) -> int:
    with web.rt.db.read() as conn:
        return conn.execute(
            "SELECT opted_in FROM memberships WHERE user_id = ? AND org_id = ?", (user, org)
        ).fetchone()[0]


def _messages(web: Web, user: str) -> list[str]:
    with web.rt.db.read() as conn:
        return [
            r[0]
            for r in conn.execute(
                "SELECT message FROM notifications WHERE user_id = ? ORDER BY id DESC", (user,)
            )
        ]


# ------------------------------------------------------------------ structure
def test_no_page_or_api_handler_runs_on_the_event_loop(web: Web):
    """Handlers do blocking work (SQLite, Relate, Argon2), so each must be a plain function that
    FastAPI runs in its thread pool. Only the form dependencies may be coroutines."""
    routers = [api.router, *pages.routers]
    endpoints = [route.endpoint for router in routers for route in router.routes]
    endpoints += [r.endpoint for r in web.client.app.routes if getattr(r, "path", "") == "/healthz"]
    assert len(endpoints) > 80
    blocking = [e.__name__ for e in endpoints if inspect.iscoroutinefunction(e)]
    assert blocking == []
    ours = [  # FastAPI's own three default handlers are coroutines and do no blocking work
        h for h in web.client.app.exception_handlers.values() if h.__module__.startswith("app.")
    ]
    assert len(ours) == 7
    assert [h.__name__ for h in ours if inspect.iscoroutinefunction(h)] == []


def test_openapi_describes_the_json_api_only(web: Web):
    paths = web.get("/api/openapi.json").json()["paths"]
    assert paths and all(p.startswith("/api/v1/") for p in paths)


def test_healthz_reports_the_outbox(web: Web):
    r = web.get("/healthz")
    assert r.json() == {"status": "ok", "pending_projections": 0, "failed": 0}
    assert "no-store" not in r.headers.get("cache-control", "")
    assert web.get("/").headers["cache-control"] == "no-store"


# ------------------------------------------------------------------ placements
def test_volunteer_and_lead_answer_then_the_volunteer_leaves(web: Web):
    dev = USERS["dev"]
    web.sign_in("dev")
    version = placement(web, FOOD_BANK, dev)["version"]
    r = web.post(f"/opportunities/{FOOD_BANK}/answer", {"answer": "yes", "version": version})
    assert (r.status_code, r.headers["location"]) == (303, "/opportunities?msg=answered")
    stale = web.post(f"/opportunities/{FOOD_BANK}/answer", {"answer": "yes", "version": version})
    assert stale.status_code == 409 and "Already changed" in stale.text
    bad = web.post(f"/opportunities/{FOOD_BANK}/answer", {"answer": "maybe", "version": "x"})
    assert bad.status_code == 400 and "Answer yes or no." in bad.text
    web.sign_in("cerys")  # the project lead
    version = placement(web, FOOD_BANK, dev)["version"]
    r = web.post(
        f"/o/{DK}/projects/{FOOD_BANK}/candidates/{dev}/answer",
        {"answer": "yes", "version": version},
    )
    assert r.headers["location"] == f"/o/{DK}/projects/{FOOD_BANK}?msg=answered"
    assert placement(web, FOOD_BANK, dev)["state"] == "team"
    web.sign_in("dev")
    r = web.post(f"/opportunities/{FOOD_BANK}/leave")
    assert (r.status_code, r.headers["location"]) == (303, "/opportunities?msg=left_team")
    assert placement(web, FOOD_BANK, dev)["state"] == "left"
    assert web.post(f"/opportunities/{FOOD_BANK}/leave").status_code == 404  # not on it any more
    assert "A volunteer left the team" in _messages(web, USERS["cerys"])[0]


def test_lead_removes_a_team_member_with_a_reason(web: Web):
    bryn = USERS["bryn"]
    path = f"/o/{CS}/projects/{WINTER}/team/{bryn}/remove"
    web.sign_in("gwen")  # a volunteer of the organisation, but not the lead
    assert web.post(path, {"reason": "Rota is full"}).status_code == 403
    web.sign_in("asha")  # the lead
    short = web.post(path, {"reason": "x"})
    assert short.status_code == 400 and "Give the volunteer a reason" in short.text
    assert placement(web, WINTER, bryn)["state"] == "team"
    r = web.post(path, {"reason": "  Rota is full  "})
    assert r.headers["location"] == f"/o/{CS}/projects/{WINTER}?msg=removed"
    assert placement(web, WINTER, bryn)["state"] == "removed"
    assert _messages(web, bryn)[0].endswith("“Winter Outreach Kitchen”: Rota is full")
    edges = web.rt.graph.read(
        "MATCH (:Volunteer {id: $v})-[:ON_TEAM]->(p:Project) RETURN p.id AS id", {"v": bryn}
    )
    assert edges == []  # the drain after the form removed the team edge


def test_org_admin_removes_a_volunteer_from_the_organisation(web: Web):
    dev = USERS["dev"]
    path = f"/o/{DK}/volunteers/{dev}/remove"
    web.sign_in("kofi")  # org admin of the other organisation
    assert web.post(path, {"reason": "No longer available"}).status_code == 403
    web.sign_in("rhiannon")
    assert web.post(path, {"reason": ""}).status_code == 400
    assert _membership(web, dev, DK) == 1
    r = web.post(path, {"reason": "No longer available"})
    assert (r.status_code, r.headers["location"]) == (303, f"/o/{DK}/volunteers?msg=removed")
    assert _membership(web, dev, DK) == 0
    assert placement(web, FOOD_BANK, dev)["state"] == "removed"
    assert _messages(web, dev)[0] == ("DataKind UK removed you as a volunteer: No longer available")
    assert 'data-volunteer="Dev"' not in web.get(f"/o/{DK}/volunteers").text
    assert web.post(path, {"reason": "No longer available"}).status_code == 404  # already gone


# ------------------------------------------------------------------ invalid input re-renders
def test_invalid_review_and_withdrawal_re_render_the_suggestion(web: Web):
    web.sign_in("rhiannon")
    r = _review(web, DK, AUDIT, "approved", reason="no")
    assert r.status_code == 400 and "Give the author a reason" in r.text
    assert "Outreach Data Audit" in r.text and _state(web, AUDIT)["state"] == "submitted"
    assert _review(web, DK, AUDIT, "approved").status_code == 303
    version = _state(web, AUDIT)["version"]
    r = web.post(f"/o/{DK}/projects/{AUDIT}/withdraw", {"reason": "", "version": version})
    assert r.status_code == 400 and "Give a reason for withdrawing" in r.text
    assert _state(web, AUDIT)["state"] == "published"
    r = web.post(f"/o/{DK}/projects/{AUDIT}/withdraw", {"reason": "Superseded", "version": version})
    assert r.headers["location"] == f"/submissions/{AUDIT}?msg=withdrawn"
    assert _state(web, AUDIT)["state"] == "withdrawn"


def test_invalid_revision_re_renders_the_suggestion(web: Web):
    web.sign_in("rhiannon")
    _review(web, DK, AUDIT, "changes_requested")
    web.sign_in("asha")
    version = _state(web, AUDIT)["version"]
    r = web.post(f"/submissions/{AUDIT}/revise", {**DRAFT, "title": "ab", "version": version})
    assert r.status_code == 400 and "Title must be 3–120 characters." in r.text
    assert _state(web, AUDIT)["state"] == "changes_requested"
    r = web.post(f"/submissions/{AUDIT}/revise", {**DRAFT, "version": version})
    assert r.headers["location"] == f"/submissions/{AUDIT}?msg=revised"


def test_invalid_suggestion_and_profile_re_render_their_forms(web: Web):
    web.sign_in("asha")
    r = web.post("/submissions/new", {**DRAFT, "org_id": DK, "summary": "short"})
    assert r.status_code == 400 and "Summary must be 10–2000 characters." in r.text
    assert 'value="Allotment Data Map"' in r.text  # what was typed is kept
    r = web.post("/profile", {"display_name": "", "version": 0})
    assert r.status_code == 400 and "Display name must be 1–60 characters." in r.text


def test_sign_in_and_sign_up_errors_re_render_with_the_email_kept(web: Web):
    token = web.csrf("/signin")
    r = web.client.post(
        "/signin",
        data={"email": "asha@cyberscallywags.uk", "password": "wrong", "csrf_token": token},
    )
    assert r.status_code == 400 and "Wrong email or password." in r.text
    assert 'value="asha@cyberscallywags.uk"' in r.text
    token = web.csrf("/signup")
    r = web.client.post(
        "/signup",
        data={"email": "asha@cyberscallywags.uk", "display_name": "Asha Again",
              "password": DEMO_PASSWORD, "csrf_token": token},
    )  # fmt: skip
    assert r.status_code == 400 and "already exists" in r.text


# ------------------------------------------------------------------ shared error pages
def test_forms_need_a_session_and_a_csrf_token(web: Web):
    r = web.client.post(f"/orgs/{DK}/join", data={}, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, f"/signin?next=%2Forgs%2F{DK}%2Fjoin")
    web.sign_in("bryn")
    r = web.client.post(f"/orgs/{DK}/join", data={"csrf_token": "forged"}, follow_redirects=False)
    assert r.status_code == 403 and "Form expired" in r.text
    assert web.client.post("/signout", data={}, follow_redirects=False).status_code == 403
    with web.rt.db.read() as conn:
        assert conn.execute(
            "SELECT 1 FROM memberships WHERE user_id = ? AND org_id = ?", (USERS["bryn"], DK)
        ).fetchone() is None  # fmt: skip
    r = web.client.post("/signin", data={"email": "x", "password": "y", "csrf_token": "forged"})
    assert r.status_code == 403


def test_error_pages_keep_the_signed_in_frame_and_look_up_the_session_once(web: Web, monkeypatch):
    from app import auth

    web.sign_in("asha")
    calls = []
    real = auth.load_session
    monkeypatch.setattr(auth, "load_session", lambda *a, **kw: calls.append(1) or real(*a, **kw))
    r = web.get("/o/org_missing/projects")
    assert r.status_code == 404 and "Not found" in r.text and "Sign out" in r.text
    assert len(calls) == 1  # the route and the error page share one lookup
    r = web.get(f"/o/{DK}/review")
    assert r.status_code == 403 and "reviewers and admins" in r.text


def test_graph_trouble_is_a_503_page_and_json(web: Web, monkeypatch):
    from tests.test_api import _token

    web.sign_in("asha")
    monkeypatch.setattr(web.rt.graph, "max_rows", 0)  # any row is now over the bound
    r = web.get(f"/o/{DK}/projects")
    assert r.status_code == 503 and "Matching unavailable" in r.text
    api = web.get(f"/api/v1/orgs/{DK}/projects", headers=_token(web, "asha"))
    assert api.status_code == 503 and "could not answer" in api.json()["detail"]

    def busy(*_a, **_kw):
        raise GraphBusy("locked")

    monkeypatch.setattr(web.rt.graph, "read", busy)
    assert "matching graph is busy" in web.get(f"/o/{DK}/projects").text


def test_project_detail_is_one_project_or_not_found(web: Web):
    web.sign_in("asha")
    page = web.get(f"/o/{DK}/projects/{FOOD_BANK}")
    assert page.status_code == 200 and "Food Bank Demand Dashboard" in page.text
    assert web.get(f"/o/{DK}/projects/{WINTER}").status_code == 404  # another organisation's
    assert web.get(f"/o/{DK}/projects/p_missing").status_code == 404
    assert web.get(f"/o/{DK}/projects/{AUDIT}").status_code == 404  # not published yet


def test_home_shows_review_counts(web: Web):
    web.sign_in("rhiannon")
    assert "Review queue (1 waiting)" in web.get("/home").text
    web.sign_in("kofi")
    assert "Review queue (0 waiting)" in web.get("/home").text


# ------------------------------------------------------------------ cost per page
def test_pages_open_few_sqlite_connections(web: Web, monkeypatch):
    """A guard against the per-request connection count creeping back up. The heaviest page
    opened 14 before the page frame and the access check were each reduced to one."""
    opened: list[int] = []
    real = sqlstore.connect
    monkeypatch.setattr(sqlstore, "connect", lambda path: opened.append(1) or real(path))

    def cost(path: str) -> int:
        opened.clear()
        assert web.get(path).status_code == 200
        return len(opened)

    web.sign_in("rhiannon")
    assert cost(f"/o/{DK}/projects/{FOOD_BANK}") <= 9
    assert cost("/notifications") <= 3
    web.sign_in("asha")
    assert cost("/home") <= 8
    assert cost("/matches") <= 5


def test_inline_projection_failures_are_logged_not_raised(web: Web, monkeypatch, caplog):
    from tests.test_api import _token

    def broken(**_kw):
        raise RuntimeError("projector down")

    monkeypatch.setattr(web.rt.projector, "drain", broken)
    with caplog.at_level(logging.ERROR, logger="app.web.deps"):
        r = web.client.put(f"/api/v1/orgs/{DK}/membership", headers=_token(web, "bryn"))
        assert r.status_code == 200 and r.json() == {"opted_in": True}
        web.sign_in("gwen")
        assert web.post(f"/orgs/{DK}/join").status_code == 303
    logged = [r for r in caplog.records if "inline projection failed" in r.getMessage()]
    assert len(logged) == 2  # the API path used to swallow this silently
