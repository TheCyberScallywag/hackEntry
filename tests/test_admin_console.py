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
from tests.test_rbac_onboarding import NIA, placement


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

    def _record(self, name: str):
        return lambda *args, **_kw: self.drawn.append((name, args))

    def __getattr__(self, name: str):
        if name in {"set_page_config", "logo", "title", "subheader", "write", "caption", "markdown",
                    "error", "warning", "success", "dataframe", "metric"}:  # fmt: skip
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

    def _pick(self, label: str, options: list):
        chosen = self.choices.get(label)
        if callable(chosen):
            return chosen(options)
        if chosen is not None:
            return chosen
        return options[0] if options else None

    def selectbox(self, label: str, options, **_kw):
        return self._pick(label, list(options))

    def radio(self, label: str, options, **_kw):
        return self._pick(label, list(options))

    def text_input(self, label: str, **_kw) -> str:
        return self.typed.get(label, "")

    def form_submit_button(self, label: str, type: str = "secondary") -> bool:  # noqa: A002
        return label in self.clicked

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
                monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
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

    monkeypatch.setattr(admin.httpx, "request", request)

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


def sign_in(console, key: str) -> None:
    _admin, st, run = console
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
        "sam": ("Volunteer screening", "Organisations", "Users and roles", "System"),
        "mo": ("Volunteer screening",),
        "rhiannon": ("Organisations",),
    }
    for key, tabs in expected.items():
        sign_in(console, key)
        assert run() is False
        assert dict(st.drawn)["tabs"] == tabs, key
        assert st.text("error") == ""
    sign_in(console, "asha")
    assert run() is False and "tabs" not in dict(st.drawn)
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
    sign_in(console, "sam")
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
    sign_in(console, "sam")
    assert run() is False
    metrics = [args for name, args in st.drawn if name == "metric"]
    assert metrics == [("Pending graph updates", 0), ("Failing (3+ attempts)", 0)]
    tables = [args[0] for name, args in st.drawn if name == "dataframe"]
    assert {"label": "Volunteer", "nodes": 11} in tables[-3]
    assert {"type": "ON_TEAM", "relationships": 1} in tables[-2]
    assert any(row["eligible_volunteers"] == "coverage gap" for row in tables[-1])
    st.clicked = {"Replay outbox now"}
    assert run() is True and st.session_state["flash"] == ("success", "Replay: done")
    assert ("POST", "/admin/projection/replay") in admin.calls


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

    monkeypatch.setattr(admin.httpx, "request", down)
    assert run() is False and "API unreachable" in st.text("error")
    st.session_state.clear()
    st.typed, st.clicked = {"Email": "sam@cyberscallywags.uk", "Password": "x"}, {"Sign in"}
    assert run() is False and "API unreachable" in st.text("error")
