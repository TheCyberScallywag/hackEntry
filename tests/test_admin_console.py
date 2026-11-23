"""The Streamlit admin console, driven without Streamlit: a small stand-in records what the
console draws and plays the clicks, while every API call goes to the real app. This checks the
console against the API's actual response shapes, which is where it would break."""

import contextlib
import importlib
import sys
import types
from typing import Any

import pytest

from tests.conftest import AUDIT, CS, DK, FOOD_BANK, USERS, Web
from tests.test_rbac_onboarding import NIA, as_, placement


class Rerun(Exception):
    """Streamlit's st.rerun() stops the script; so does this."""


class _Column:
    def __init__(self, st: FakeStreamlit) -> None:
        self._st = st

    def __getattr__(self, name: str):
        return getattr(self._st, name)

    def __enter__(self) -> _Column:
        return self

    def __exit__(self, *_exc) -> None:
        return None


class FakeStreamlit:
    """Just enough of the Streamlit API for app.admin.main."""

    def __init__(self) -> None:
        self.session_state: dict[str, Any] = {}
        self.drawn: list[tuple[str, tuple]] = []  # (widget, arguments), in order
        self.choices: dict[str, Any] = {}  # widget label -> value (or a function of the options)
        self.typed: dict[str, str] = {}  # text input label -> text
        self.clicked: set[str] = set()  # button labels or keys
        self.sidebar = _Column(self)
        self.section: str | None = None  # the sidebar's "Section"; None: the first one offered

    def _record(self, name: str):
        return lambda *args, **_kw: self.drawn.append((name, args))

    def __getattr__(self, name: str):
        if name in {"set_page_config", "logo", "title", "subheader", "write", "caption", "markdown",
                    "error", "warning", "success", "dataframe", "metric", "code"}:  # fmt: skip
            return self._record(name)
        raise AttributeError(name)

    def text(self, widget: str) -> str:
        return " ".join(str(a) for name, args in self.drawn if name == widget for a in args)

    @contextlib.contextmanager
    def form(self, _key: str):
        yield

    def tabs(self, names: list[str]) -> list[_Column]:
        self.drawn.append(("tabs", tuple(names)))
        return [_Column(self) for _ in names]

    def columns(self, spec) -> list[_Column]:
        return [_Column(self) for _ in (range(spec) if isinstance(spec, int) else spec)]

    def _pick(self, label: str, options: list, index: int = 0):
        chosen = self.choices.get(label)
        if callable(chosen):
            return chosen(options)
        if chosen is not None:
            return chosen
        return options[index] if options else None

    def selectbox(self, label: str, options, index: int = 0, **_kw):
        return self._pick(label, list(options), index)

    def radio(self, label: str, options, index: int = 0, **_kw):
        if label == "Section":
            self.drawn.append(("sections", tuple(options)))
            return self.section if self.section in options else list(options)[0]
        return self._pick(label, list(options), index)

    def text_input(self, label: str, value: str = "", **_kw) -> str:
        return self.typed.get(label, value)

    text_area = text_input

    def checkbox(self, label: str, value: bool = False, **_kw) -> bool:
        return self.choices.get(label, value)

    def form_submit_button(self, label: str, type: str = "secondary") -> bool:  # noqa: A002
        return label in self.clicked

    def popover(self, _label: str) -> _Column:
        return _Column(self)

    def button(self, label: str, key: str | None = None) -> bool:
        return label in self.clicked or (key is not None and key in self.clicked)

    def rerun(self) -> None:
        raise Rerun


@pytest.fixture
def console(web: Web, monkeypatch):
    """(module, fake streamlit, run): `run()` executes the console once, like one script run."""
    for name in ("streamlit", "pandas"):  # optional dependencies; absent from the dev install
        if name not in sys.modules:
            try:
                importlib.import_module(name)
            except ModuleNotFoundError:
                stub = types.ModuleType(name)
                # Logfire looks these up on any `pandas` it finds loaded, to describe values.
                stub.DataFrame = stub.Series = type("Absent", (), {})
                monkeypatch.setitem(sys.modules, name, stub)
    monkeypatch.delitem(sys.modules, "app.admin.main", raising=False)
    admin = importlib.import_module("app.admin.main")
    st = FakeStreamlit()
    monkeypatch.setattr(admin, "st", st)
    monkeypatch.setattr(admin, "pd", types.SimpleNamespace(DataFrame=lambda rows: rows))
    calls: list[tuple[str, str]] = []

    def request(method: str, url: str, **kwargs):
        kwargs.pop("timeout", None)
        path = "/api/v1" + url.removeprefix(admin.API)
        calls.append((method, path.removeprefix("/api/v1")))
        return web.client.request(method, path, **kwargs)

    opened: list[int] = []
    through_the_app = request

    class Client:
        def __init__(self, **_kw) -> None:
            opened.append(1)

        request = staticmethod(through_the_app)

    monkeypatch.setattr(admin.httpx, "Client", Client)
    admin.opened = opened

    def run() -> bool:
        """True if the run ended in st.rerun()."""
        st.drawn.clear()
        calls.clear()
        try:
            admin.main()
        except Rerun:
            return True
        return False

    admin.calls = calls
    yield admin, st, run
    sys.modules.pop("app.admin.main", None)


def sign_in(console, key: str, section: str | None = None) -> None:
    _admin, st, run = console
    st.section = section
    st.session_state.clear()
    st.typed = {"Email": f"{key}@cyberscallywags.uk", "Password": "demo-password-1"}
    st.clicked = {"Sign in"}
    assert run() is True and "token" in st.session_state
    st.typed, st.clicked = {}, set()


def test_sign_in_form_and_wrong_password(console):
    _admin, st, run = console
    assert run() is False and st.text("subheader") == "Staff sign in"
    st.typed = {"Email": "sam@cyberscallywags.uk", "Password": "wrong"}
    st.clicked = {"Sign in"}
    assert run() is False and st.text("error") == "Wrong email or password"
    assert "token" not in st.session_state


def test_tabs_follow_roles(console):
    admin, st, run = console
    expected = {
        "sam": (
            "Volunteer screening",
            "Organisations",
            "Onboarding",
            "Users and roles",
            "Audit",
            "System",
        ),
        "mo": ("Volunteer screening",),
        "rhiannon": ("Organisations", "Onboarding"),
    }
    for key, tabs in expected.items():
        sign_in(console, key)
        assert run() is False
        assert dict(st.drawn)["sections"] == tabs, key
        assert st.text("error") == ""
    sign_in(console, "asha")
    assert run() is False and "sections" not in dict(st.drawn)
    assert "moderators, org admins and system admins" in st.text("warning")
    assert admin.tabs_for({"system_roles": [], "organisations": []}) == []


def test_screening_fetches_one_record_and_records_a_decision(console, web: Web):
    admin, st, run = console
    sign_in(console, "mo")
    assert run() is False
    records = [c for c in admin.calls if c[1].startswith("/moderation/volunteers/")]
    assert records == [("GET", f"/moderation/volunteers/{NIA}")]  # the selected row only
    assert "Email (admins only)" not in st.text("caption")  # moderators never see emails
    assert "Cyber Scallywags" in st.text("write")
    st.clicked = {"Record decision"}  # the first offered action: approve
    assert run() is True and st.session_state["flash"] == ("success", "Vetting: done")
    with web.rt.db.read() as conn:
        assert conn.execute("SELECT status FROM users WHERE id = ?", (NIA,)).fetchone()[0] == (
            "active"
        )
    st.clicked = set()
    assert run() is False and st.text("success") == "Vetting: done"
    assert "Nobody in this status." in st.text("caption")  # the queue is now empty


def test_an_api_refusal_is_shown_and_the_console_keeps_running(console):
    _admin, st, run = console
    sign_in(console, "mo")
    st.choices = {"Decision": "decline"}  # declining needs a reason, and none is typed
    st.clicked = {"Record decision"}
    assert run() is True
    kind, message = st.session_state["flash"]
    assert kind == "error" and "Give the volunteer a reason" in message


def test_organisation_tab_reviews_and_answers_candidates(console, web: Web):
    admin, st, run = console
    sign_in(console, "rhiannon")
    st.choices = {"Suggestion": lambda queue: next(s for s in queue if s["id"] == AUDIT)}
    st.typed = {"Reason (shown to the author)": "Good to go"}
    assert run() is False
    details = [c for c in admin.calls if c[1].startswith("/submissions/")]
    assert details == [("GET", f"/submissions/{AUDIT}")]  # not one per row of the queue
    assert "Dev" in str(st.drawn) and "@" not in str([d for d in st.drawn if d[0] == "dataframe"])
    st.clicked = {"Record decision"}
    assert run() is True and st.session_state["flash"] == ("success", "Review: done")
    with web.rt.db.read() as conn:
        state = conn.execute("SELECT state FROM submissions WHERE id = ?", (AUDIT,)).fetchone()[0]
    assert state == "published"
    # a published project lists its candidates; say yes to Dev on the food bank project
    st.choices = {"Suggestion": lambda queue: next(s for s in queue if s["id"] == FOOD_BANK)}
    st.clicked = {f"{FOOD_BANK}-{USERS['dev']}-yes"}
    assert run() is True and st.session_state["flash"] == ("success", "Candidate: done")
    assert placement(web, FOOD_BANK, USERS["dev"])["lead_said"] == "yes"
    st.clicked = set()
    assert run() is False and "Coverage gaps" in st.text("subheader")


def test_organisation_tab_removes_a_volunteer(console, web: Web):
    _admin, st, run = console
    sign_in(console, "rhiannon")
    st.choices = {"Remove a volunteer": lambda people: next(
        p for p in people if p["id"] == USERS["dev"]
    )}  # fmt: skip
    st.typed = {"Reason (shown to the volunteer)": "Moved away"}
    st.clicked = {"Remove from organisation"}
    assert run() is True and st.session_state["flash"] == ("success", "Remove volunteer: done")
    with web.rt.db.read() as conn:
        opted_in = conn.execute(
            "SELECT opted_in FROM memberships WHERE user_id = ? AND org_id = ?",
            (USERS["dev"], DK),
        ).fetchone()[0]
    assert opted_in == 0


def test_users_tab_grants_roles_and_creates_an_organisation(console, web: Web):
    _admin, st, run = console
    sign_in(console, "sam", "Users and roles")
    gwen = USERS["gwen"]
    st.choices = {
        "User": lambda users: next(u for u in users if u["id"] == gwen),
        "Role": lambda choices: next(c for c in choices if c[0] == "Cyber Scallywags: reviewer"),
    }
    st.clicked = {"Apply"}
    assert run() is True and st.session_state["flash"] == ("success", "Role: done")

    def roles() -> list[tuple]:
        with web.rt.db.read() as conn:
            return [
                tuple(r)
                for r in conn.execute(
                    "SELECT org_id, role FROM org_roles WHERE user_id = ?", (gwen,)
                )
            ]

    assert roles() == [(CS, "reviewer")]
    st.choices["Change"] = "revoke"
    assert run() is True and roles() == []
    st.choices["Role"] = lambda choices: next(c for c in choices if c[0] == "system: moderator")
    st.choices["Change"] = "grant"
    assert run() is True
    with web.rt.db.read() as conn:
        assert conn.execute(
            "SELECT 1 FROM system_roles WHERE user_id = ? AND role = 'moderator'", (gwen,)
        ).fetchone()
    st.clicked = {"Create"}
    st.typed = {"Key (lower-case, hyphens)": "new-org", "Name": "New Org", "Description": "Demo"}
    assert run() is True and st.session_state["flash"] == ("success", "Organisation: done")


def test_system_tab_shows_projection_and_graph_and_replays(console):
    admin, st, run = console
    sign_in(console, "sam", "System")
    assert run() is False
    metrics = [args for name, args in st.drawn if name == "metric"]
    assert metrics[-2:] == [("Pending graph updates", 0), ("Failing (3+ attempts)", 0)]
    tables = [args[0] for name, args in st.drawn if name == "dataframe"]
    assert {"label": "Volunteer", "nodes": 11} in tables[-3]
    assert {"type": "ON_TEAM", "relationships": 5} in tables[-2]
    assert any(row["eligible_volunteers"] == "coverage gap" for row in tables[-1])
    st.clicked = {"Replay outbox now"}
    assert run() is True and st.session_state["flash"] == ("success", "Replay: done")
    assert ("POST", "/admin/projection/replay") in admin.calls


def test_audit_tab_shows_checks_roles_and_the_trail(console, web: Web):
    admin, st, run = console
    as_(web, "asha").get("/admin/users")  # a refusal to find in the trail
    sign_in(console, "sam", "Audit")
    st.choices = {"Show": "Refusals"}
    assert run() is False and st.text("error") == ""
    assert ("Refused", 1) in [args for name, args in st.drawn if name == "metric"]
    tables = [args[0] for name, args in st.drawn if name == "dataframe"]
    checks = next(t for t in tables if t and "evidence" in t[0])
    assert {c["title"] for c in checks} == {"Only one system admin", "An organisation has no admin"}
    holders = next(t for t in tables if t and "scope" in t[0])
    assert {"scope": "Cyber Scallywags", "name": "Kofi", "role": "org_admin"}.items() <= (
        next(h for h in holders if h["name"] == "Kofi").items()
    )
    trail = next(t for t in tables if t and "outcome" in t[0])
    assert [(e["who"], e["action"], e["outcome"]) for e in trail] == [
        ("Asha", "http.get", "denied")
    ]
    assert ("GET", "/admin/audit/events") in admin.calls
    assert "@" not in str([checks, holders, trail])  # names, never addresses


def test_audit_tab_runs_the_audit_assistant(console, web: Web):
    from tests.agent_models import Scripted, final
    from tests.test_agents import BOTH, REPORT

    admin, st, run = console
    sign_in(console, "sam", "Audit")
    st.clicked = {"audit-assistant"}
    assert run() is False
    assert "The assistants are not configured" in st.text("error")

    web.rt.agent_model = Scripted(final(**REPORT, findings=BOTH))
    assert run() is False and st.text("error") == ""
    assert "**Attention.** Two things need attention." in st.text("write")
    assert st.text("markdown").count("**medium: A finding.**") == 2
    assert ("POST", "/agents/audit") in admin.calls


def test_a_click_fetches_only_the_section_on_screen_over_one_connection(console):
    admin, st, run = console
    sign_in(console, "sam", "Users and roles")
    assert run() is False
    assert sorted(set(admin.calls)) == [("GET", "/admin/users"), ("GET", "/me")]
    st.section = "System"
    assert run() is False
    assert ("GET", "/admin/graph") in admin.calls and ("GET", "/admin/users") not in admin.calls
    assert not [c for c in admin.calls if "audit" in c[1] or "moderation" in c[1]]
    assert admin.opened == [1]  # sign-in and every rerun since shared one client


def test_an_error_page_from_a_proxy_is_reported_not_raised(console, monkeypatch):
    admin, st, run = console
    sign_in(console, "sam")

    class BadGateway:
        status_code, content, text = 502, b"<html>Bad gateway</html>", "<html>Bad gateway</html>"

        def json(self):
            raise ValueError("not JSON")

    st.session_state["client"] = types.SimpleNamespace(request=lambda *_a, **_kw: BadGateway())
    assert run() is False and "The API answered 502." in st.text("error")


def test_sign_out_and_an_expired_session_return_to_the_sign_in_form(console, web: Web):
    admin, st, run = console
    sign_in(console, "sam")
    st.clicked = {"Sign out"}
    assert run() is True and st.session_state == {}
    assert ("DELETE", "/session") in admin.calls
    sign_in(console, "sam")
    with web.rt.db.tx() as conn:
        conn.execute("UPDATE sessions SET revoked_at = created_at WHERE kind = 'bearer'")
    assert run() is True and st.session_state == {}  # a 401 clears the token


def test_an_unreachable_api_is_reported(console, monkeypatch):
    admin, st, run = console
    sign_in(console, "sam")

    def down(*_a, **_kw):
        raise admin.httpx.ConnectError("refused")

    st.session_state["client"] = types.SimpleNamespace(request=down)
    assert run() is False and "API unreachable" in st.text("error")
    st.session_state.clear()
    st.session_state["client"] = types.SimpleNamespace(request=down)
    st.typed, st.clicked = {"Email": "sam@cyberscallywags.uk", "Password": "x"}, {"Sign in"}
    assert run() is False and "API unreachable" in st.text("error")


# ------------------------------------------------------------------ onboarding workflows
SWITCH = "Switched on: volunteers who join from now on go through these steps"


def _flow(web: Web, org: str = CS) -> dict:
    return as_(web, "kofi").get(f"/orgs/{org}/onboarding").json()


def test_onboarding_section_edits_the_workflow_steps_and_templates(console, web: Web):
    admin, st, run = console
    sign_in(console, "kofi", "Onboarding")
    assert run() is False and st.text("error") == ""
    assert admin.calls == [("GET", "/me"), ("GET", f"/orgs/{CS}/onboarding")]  # this view only
    steps = next(args[0] for name, args in st.drawn if name == "dataframe")
    assert steps[0] == {"step": 1, "title": "Read our code of conduct",
                        "completed by": "The volunteer", "email": "Welcome"}  # fmt: skip
    assert "{{ volunteer_name }}: The volunteer's display name" in st.text("caption")

    st.choices = {SWITCH: True}
    st.clicked = {"Save settings"}
    assert run() is True and st.session_state["flash"] == ("success", "Workflow: done")
    assert _flow(web)["enabled"] is True and _flow(web)["completion_template_id"]  # kept

    st.choices = {}
    st.typed = {"Title": "Safeguarding briefing", "What the volunteer is asked to do": "Watch it."}
    st.clicked = {"Add step"}
    assert run() is True and st.session_state["flash"] == ("success", "Step: done")
    added = _flow(web)["steps"][-1]
    assert (added["title"], added["completed_by"], added["email_template_id"]) == (
        "Safeguarding briefing", "volunteer", None,
    )  # fmt: skip

    # edit it: the form starts from what is saved, so only the changed field is typed
    st.choices = {"Step": lambda steps: next(s for s in steps if s["id"] == added["id"]),
                  "Completed by": "org_admin"}  # fmt: skip
    st.typed = {}
    st.clicked = {"Save step"}
    assert run() is True
    edited = _flow(web)["steps"][-1]
    assert (edited["title"], edited["description"], edited["completed_by"]) == (
        "Safeguarding briefing", "Watch it.", "org_admin",
    )  # fmt: skip
    st.choices.pop("Completed by")
    st.clicked = {"onboarding-up"}
    assert run() is True
    assert [s["id"] for s in _flow(web)["steps"]].index(added["id"]) == 1
    st.clicked = {"onboarding-delete-step"}
    assert run() is True and len(_flow(web)["steps"]) == 2

    # templates: preview, save, test, delete
    st.choices = {}
    st.typed = {"Name": "Induction", "Subject": "Hello {{ volunteer_name }}",
                "Text": "Welcome to {{ organisation_name }}."}  # fmt: skip
    st.clicked = {"Preview"}
    assert run() is False
    assert "**Subject:** Hello Alex Example" in st.text("markdown")
    assert st.text("code") == "Welcome to Cyber Scallywags."
    st.clicked = {"Add template"}
    assert run() is True and st.session_state["flash"] == ("success", "Template: done")
    made = next(t for t in _flow(web)["templates"] if t["name"] == "Induction")
    st.choices = {"Template": lambda options: next(t for t in options if t["id"] == made["id"])}
    st.typed = {"Text": "Hello {{ nobody }}, welcome."}
    st.clicked = {"Save template"}
    assert run() is True
    kind, message = st.session_state["flash"]
    assert kind == "error" and "Unknown placeholder: {{ nobody }}" in message
    st.typed = {}
    st.clicked = {"onboarding-test"}
    assert run() is True and st.session_state["flash"] == ("success", "Test email: done")
    st.clicked = {"onboarding-delete-template"}
    assert run() is True
    assert "Induction" not in [t["name"] for t in _flow(web)["templates"]]


def test_onboarding_section_shows_progress_confirms_a_step_and_lists_emails(console, web: Web):
    admin, st, run = console
    kofi = as_(web, "kofi")
    flow = _flow(web)
    kofi.put(
        f"/orgs/{CS}/onboarding",
        {
            "enabled": True,
            "version": flow["version"],
            "completion_template_id": flow["completion_template_id"],
        },
    )
    sign_in(console, "kofi", "Onboarding")
    st.choices = {"Show": "Volunteers"}
    assert run() is False and "Nobody has started onboarding yet" in st.text("caption")
    dev = as_(web, "dev")
    dev.put(f"/orgs/{CS}/membership")
    dev.post(f"/orgs/{CS}/onboarding/steps/{flow['steps'][0]['id']}/complete")
    assert run() is False
    assert ("GET", f"/orgs/{CS}/onboarding") not in admin.calls  # the workflow is not fetched
    [row] = next(args[0] for name, args in st.drawn if name == "dataframe")
    assert (row["volunteer"], row["steps done"], row["current step"], row["waiting for"]) == (
        "Dev", "1 of 2", "Welcome call", "An organisation admin",
    )  # fmt: skip
    st.clicked = {"Mark step as done"}
    assert run() is True and st.session_state["flash"] == ("success", "Onboarding step: done")
    assert dev.get("/me/onboarding").json()[0]["state"] == "completed"
    st.clicked = set()
    st.choices = {"Show": "Emails"}
    assert run() is False
    table = next(args[0] for name, args in st.drawn if name == "dataframe")
    assert [e["to"] for e in table] == ["Dev"] * 3 and "@" not in str(table)
    assert {e["status"] for e in table} == {"Queued"}


TEAM_SCOPE = "Volunteers joining a project's team"


def test_onboarding_section_edits_a_projects_team_onboarding(console, web: Web):
    admin, st, run = console
    base = f"/orgs/{DK}/projects/{FOOD_BANK}/onboarding"
    sign_in(console, "rhiannon", "Onboarding")
    st.choices = {"Workflow for": TEAM_SCOPE}
    assert run() is False and st.text("error") == ""
    assert admin.calls == [("GET", "/me"), ("GET", f"/orgs/{DK}/submissions"), ("GET", base)]
    steps = next(args[0] for name, args in st.drawn if name == "dataframe")
    assert steps[1] == {
        "step": 2,
        "title": "Intro call with the project lead",
        "completed by": "The project lead or an org admin",
        "email": "",
    }
    assert "Name" not in str(st.drawn)  # templates are the organisation's: not edited here
    st.choices |= {SWITCH: True}
    st.clicked = {"Save settings"}
    assert run() is True and st.session_state["flash"] == ("success", "Workflow: done")
    rhiannon = as_(web, "rhiannon")
    assert rhiannon.get(base).json()["enabled"] is True
    st.choices = {"Workflow for": TEAM_SCOPE, "Completed by": "lead"}
    st.typed = {"Title": "Agree a first task"}
    st.clicked = {"Add step"}
    assert run() is True and st.session_state["flash"] == ("success", "Step: done")
    added = rhiannon.get(base).json()["steps"][-1]
    assert (added["title"], added["completed_by"]) == ("Agree a first task", "lead")

    # who is where: Dev accepts an invitation and starts the steps
    version = placement(web, FOOD_BANK, USERS["dev"])["version"]
    team = f"/projects/{FOOD_BANK}/placements"
    rhiannon.post(f"{team}/{USERS['dev']}/answer", {"answer": "yes", "version": version})
    as_(web, "dev").post(
        f"/placements/{FOOD_BANK}/answer", {"answer": "yes", "version": version + 1}
    )
    st.choices = {"Workflow for": TEAM_SCOPE, "Show": "Volunteers"}
    st.typed, st.clicked = {}, {"Mark step as done"}
    assert run() is True and st.session_state["flash"] == ("success", "Onboarding step: done")
    st.clicked = set()
    assert run() is False
    [row] = next(args[0] for name, args in st.drawn if name == "dataframe")
    assert (row["volunteer"], row["steps done"], row["waiting for"]) == (
        "Dev", "1 of 3", "The project lead or an org admin",
    )  # fmt: skip


def test_organisation_tab_assigns_the_lead_and_accepts_suspends_and_removes(console, web: Web):
    _admin, st, run = console
    rhiannon, dev = as_(web, "rhiannon"), USERS["dev"]
    base = f"/orgs/{DK}/projects/{FOOD_BANK}/onboarding"
    flow = rhiannon.get(base).json()
    rhiannon.put(base, {"enabled": True, "version": flow["version"]})
    version = placement(web, FOOD_BANK, dev)["version"]
    rhiannon.post(f"/projects/{FOOD_BANK}/placements/{dev}/answer",
                  {"answer": "yes", "version": version})  # fmt: skip
    as_(web, "dev").post(
        f"/placements/{FOOD_BANK}/answer", {"answer": "yes", "version": version + 1}
    )

    sign_in(console, "rhiannon")
    food_bank = {"Suggestion": lambda queue: next(s for s in queue if s["id"] == FOOD_BANK)}
    st.choices = dict(food_bank)
    assert run() is False and "2 steps left" in st.text("caption")
    for step in flow["steps"]:
        rhiannon.post(f"{base}/volunteers/{dev}/steps/{step['id']}/complete")
    key = f"{FOOD_BANK}-{dev}"
    st.clicked = {f"{key}-accept"}
    assert run() is True and st.session_state["flash"] == ("success", "Team: done")
    assert placement(web, FOOD_BANK, dev)["state"] == "team"
    st.typed = {"Reason (shown to the volunteer)": "Missed three sessions"}
    st.clicked = {f"{key}-suspend"}
    assert run() is True and placement(web, FOOD_BANK, dev)["state"] == "suspended"
    st.clicked = {f"{key}-reinstate"}
    assert run() is True and placement(web, FOOD_BANK, dev)["state"] == "team"
    st.clicked = {f"{key}-remove"}
    assert run() is True and placement(web, FOOD_BANK, dev)["state"] == "removed"

    # org admins assign the lead
    st.typed, st.clicked = {}, {"Assign lead"}
    st.choices = food_bank | {
        "Project lead": lambda people: next(p for p in people if p["id"] == USERS["kofi"])
    }
    assert run() is True and st.session_state["flash"] == ("success", "Project lead: done")
    with web.rt.db.read() as conn:
        lead = conn.execute("SELECT lead_id FROM submissions WHERE id = ?", (FOOD_BANK,)).fetchone()
    assert lead[0] == USERS["kofi"]
