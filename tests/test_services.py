"""The service layer's shared pieces: access in one query, grouped listings, small helpers."""

import pytest

from app import services
from tests.conftest import AUDIT, CS, DK, IL, USERS, Web
from tests.test_rbac_onboarding import MO, SAM
from tests.test_submissions import DRAFT, _review, _state


def _access(web: Web, user_id: str, org_id: str) -> services.Access:
    with web.rt.db.read() as conn:
        return services.org_access(conn, user_id, org_id)


def test_org_access_for_each_kind_of_standing(web: Web):
    asha = _access(web, USERS["asha"], DK)
    assert (asha.volunteer, asha.reviewer, asha.org_admin, asha.system_admin) == (
        True, False, False, False,
    )  # fmt: skip
    rhiannon = _access(web, USERS["rhiannon"], DK)  # org admin, not a volunteer
    assert (rhiannon.volunteer, rhiannon.reviewer, rhiannon.org_admin) == (False, True, True)
    assert not _access(web, USERS["rhiannon"], CS).any  # her role is for DataKind UK only
    kofi = _access(web, USERS["kofi"], DK)  # org admin elsewhere, plain volunteer here
    assert (kofi.volunteer, kofi.reviewer, kofi.org_admin) == (True, False, False)
    sam = _access(web, SAM, CS)  # system admins hold every organisation role
    assert (sam.volunteer, sam.reviewer, sam.org_admin, sam.system_admin) == (
        False, True, True, True,
    )  # fmt: skip
    assert not _access(web, MO, DK).any  # a moderator has no organisation standing
    assert sam.allows("reviewer") and not asha.allows("org_admin") and asha.allows("any")


def test_org_access_reviewer_role_alone(web: Web):
    with web.rt.db.tx() as conn:
        conn.execute(
            "INSERT INTO org_roles (user_id, org_id, role, granted_at) "
            "VALUES (?, ?, 'reviewer', ?)",
            (USERS["gwen"], DK, "2026-01-01T00:00:00+00:00"),
        )
    gwen = _access(web, USERS["gwen"], DK)
    assert (gwen.volunteer, gwen.reviewer, gwen.org_admin) == (False, True, False)


def test_org_access_fails_closed_for_unknown_ids(web: Web):
    with pytest.raises(services.NotFound, match="organisation"):
        _access(web, USERS["asha"], "org_missing")
    with pytest.raises(services.NotFound, match="user"):
        _access(web, "u_missing", DK)


def test_org_access_is_a_single_query(web: Web):
    statements: list[str] = []
    with web.rt.db.read() as conn:
        conn.set_trace_callback(statements.append)
        services.org_access(conn, USERS["asha"], DK)
        conn.set_trace_callback(None)
    assert len(statements) == 1


def test_list_users_groups_roles_without_a_query_per_user(web: Web):
    statements: list[str] = []
    real = web.rt.db.read

    def traced():
        ctx = real()
        conn = ctx.__enter__()
        conn.set_trace_callback(statements.append)

        class _Ctx:
            def __enter__(self):
                return conn

            def __exit__(self, *exc):
                return ctx.__exit__(*exc)

        return _Ctx()

    web.rt.db.read = traced
    users = {u["id"]: u for u in services.list_users(web.rt.db, SAM)}
    web.rt.db.read = real
    assert len(users) == 11
    assert len([s for s in statements if s.lstrip().upper().startswith("SELECT")]) <= 6
    assert users[SAM]["system_roles"] == ["admin"] and users[MO]["system_roles"] == ["moderator"]
    assert users[USERS["kofi"]]["org_roles"] == [
        {"org_id": CS, "org": "Cyber Scallywags", "role": "org_admin"}
    ]
    assert users[USERS["asha"]]["system_roles"] == [] and users[USERS["asha"]]["org_roles"] == []
    assert users[USERS["asha"]]["email"] == "asha@cyberscallywags.uk"
    with pytest.raises(services.Forbidden):
        services.list_users(web.rt.db, USERS["rhiannon"])


def test_review_counts_per_organisation_the_user_reviews_for(web: Web):
    db = web.rt.db
    assert services.review_counts(db, USERS["rhiannon"]) == {DK: 1}  # the seeded audit suggestion
    assert services.review_counts(db, USERS["kofi"]) == {CS: 0}
    assert services.review_counts(db, USERS["asha"]) == {}
    assert services.review_counts(db, SAM) == {DK: 1, CS: 0, IL: 0}
    for user in ("rhiannon", "kofi"):  # the same numbers the full queues give
        for org, waiting in services.review_counts(db, USERS[user]).items():
            queue = services.review_queue(db, USERS[user], org)
            assert waiting == sum(1 for s in queue if s["state"] == "submitted")


def test_skill_flags_and_states_round_trip():
    assert services.skill_flags("has") == (1, 0, 0)
    assert services.skill_flags("offers") == (1, 1, 0)
    assert services.skill_flags("exclude") == (0, 0, 1)
    assert services.skill_flags("none") == (0, 0, 0)
    for state in services.SKILL_STATES:
        assert services.skill_state(*services.skill_flags(state)) == state


def test_require_reason_trims_and_bounds():
    assert services.require_reason("  fine  ") == "fine"
    with pytest.raises(
        services.Invalid, match=r"Give the volunteer a reason \(3–1000 characters\)"
    ):
        services.require_reason(" x ")
    with pytest.raises(services.Invalid, match="Give the author a reason"):
        services.require_reason("y" * (services.REASON_MAX + 1), "Give the author a reason")


def test_role_guards(web: Web):
    db = web.rt.db
    assert services.require_admin(db, SAM).admin
    assert services.require_vetter(db, MO).can_vet and services.require_vetter(db, SAM).can_vet
    with pytest.raises(services.Forbidden, match="admin"):
        services.require_admin(db, MO)
    with pytest.raises(services.Forbidden, match="moderator"):
        services.require_vetter(db, USERS["asha"])


def test_a_revision_tells_the_reviewers_it_is_back_in_their_queue(web: Web):
    web.sign_in("rhiannon")
    assert _review(web, DK, AUDIT, "changes_requested").status_code == 303
    with web.rt.db.tx() as conn:
        conn.execute("DELETE FROM notifications WHERE user_id = ?", (USERS["rhiannon"],))
    web.sign_in("asha")
    data = {**DRAFT, "title": "Outreach Data Audit v2", "version": _state(web, AUDIT)["version"]}
    assert web.post(f"/submissions/{AUDIT}/revise", data).status_code == 303
    with web.rt.db.read() as conn:
        rows = conn.execute(
            "SELECT kind, message, link FROM notifications WHERE user_id = ?",
            (USERS["rhiannon"],),
        ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("review", "“Outreach Data Audit v2” was revised and is back in review.",
         f"/submissions/{AUDIT}"),
    ]  # fmt: skip


def test_submission_detail_names_its_columns(web: Web):
    sub = services.get_submission(web.rt.db, USERS["rhiannon"], AUDIT)
    assert sub["org_name"] == "DataKind UK" and sub["author_name"] == "Asha"
    assert sub["approved_by_name"] is None and sub["can_review"] is True
    assert "withdrawn_by" not in sub  # an internal column the page and API never used
    web.sign_in("rhiannon")
    _review(web, DK, AUDIT, "approved")
    assert services.get_submission(web.rt.db, USERS["asha"], AUDIT)["approved_by_name"] == (
        "Rhiannon"
    )
