"""The audit trail: role changes, requests, sign-ins, and the checks built on them."""

import json

import pytest

from app import audit, cli, services
from app.config import Settings
from app.runtime import reset_demo
from app.sqlstore import days_ago, prune
from tests.conftest import CS, DK, USERS, Web
from tests.test_rbac_onboarding import MO, SAM, as_


def events(web: Web, **params) -> list[dict]:
    r = as_(web, "sam").get("/admin/audit/events", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_a_role_change_and_its_audit_row_commit_together(web: Web):
    sam = as_(web, "sam")
    assert sam.put(f"/admin/orgs/{CS}/roles/{USERS['bryn']}/reviewer").status_code == 200
    assert sam.put(f"/admin/users/{USERS['bryn']}/system-roles/moderator").status_code == 200
    grants = events(web, action="role.grant")
    assert [(e["actor"], e["target_id"], e["org"], e["detail"]) for e in grants] == [
        ("Sam", USERS["bryn"], None, {"role": "moderator", "scope": "system"}),
        ("Sam", USERS["bryn"], "Cyber Scallywags", {"role": "reviewer", "scope": "organisation"}),
    ]
    assert sam.delete(f"/admin/users/{USERS['bryn']}/system-roles/moderator").status_code == 200
    [revoked] = events(web, action="role.revoke")
    assert revoked["detail"]["role"] == "moderator" and revoked["outcome"] == "ok"


def test_granting_a_role_twice_tells_nobody_twice(web: Web):
    sam, bryn = as_(web, "sam"), as_(web, "bryn")
    for _ in range(3):
        assert sam.put(f"/admin/orgs/{CS}/roles/{USERS['bryn']}/reviewer").status_code == 200
    assert len(events(web, action="role.grant")) == 1
    assert sum("reviewer role" in m for m in bryn.messages()) == 1
    # Removing a role nobody holds is likewise not an event.
    assert sam.delete(f"/admin/orgs/{DK}/roles/{USERS['bryn']}/org_admin").status_code == 200
    assert events(web, action="role.revoke") == []


def test_a_refused_role_change_is_audited_as_a_refusal(web: Web):
    kofi = as_(web, "kofi")  # org admin, not a system admin
    assert kofi.put(f"/admin/orgs/{CS}/roles/{USERS['bryn']}/reviewer").status_code == 403
    assert events(web, action="role.") == []  # nothing was granted
    [refused] = events(web, outcome="denied")
    assert refused["action"] == "http.put" and refused["actor"] == "Kofi"
    assert refused["target_id"] == "/api/v1/admin/orgs/{org_id}/roles/{user_id}/{role}"
    assert refused["detail"] == {"status": 403} and refused["source"] == "api"


def test_requests_that_change_something_are_recorded_and_reads_are_not(web: Web):
    asha = as_(web, "asha")
    asha.get("/me")
    asha.get(f"/orgs/{DK}/matches")
    assert events(web, action="http.") == []
    assert asha.put("/me/active-org", {"org_id": CS}).status_code == 200
    assert asha.post("/submissions", {"org_id": DK, "title": "x"}).status_code == 422
    recorded = [(e["action"], e["outcome"], e["target_id"]) for e in events(web, action="http.")]
    assert recorded == [
        ("http.post", "invalid", "/api/v1/submissions"),
        ("http.put", "ok", "/api/v1/me/active-org"),
    ]
    assert all(e["actor"] == "Asha" for e in events(web, action="http."))


def test_a_refused_read_is_recorded(web: Web):
    as_(web, "asha").get("/admin/users")
    web.client.get("/api/v1/me")  # not signed in
    refused = [
        (e["action"], e["actor"], e["detail"]["status"]) for e in events(web, outcome="denied")
    ]
    assert refused == [("http.get", None, 401), ("http.get", "Asha", 403)]


def test_the_trail_names_routes_and_never_holds_what_was_typed(web: Web):
    web.sign_in("asha")
    web.post("/profile", {"display_name": "Asha <secret@example.org>", "version": "999"},
             csrf_from="/profile")  # fmt: skip
    [event] = events(web, action="http.post")
    assert event["source"] == "web" and event["target_id"] == "/profile"
    with web.rt.db.read() as conn:
        stored = " ".join(str(v) for row in conn.execute("SELECT * FROM audit_log") for v in row)
    assert "secret@example.org" not in stored and "cyberscallywags.uk" not in stored


def test_sign_ins_are_audited_once_each_with_no_email(web: Web):
    as_(web, "asha")
    web.client.post("/api/v1/session", json={"email": "asha@cyberscallywags.uk",
                                             "password": "wrong-password"})  # fmt: skip
    web.sign_in("bryn")
    seen = [(e["outcome"], e["actor"], e["source"]) for e in events(web, action="auth.")]
    # events() itself signs Sam in each time it is called
    assert ("denied", None, "api") in seen and ("ok", "Asha", "api") in seen
    assert ("ok", "Bryn", "web") in seen
    assert events(web, action="http.post") == []  # not recorded a second time as a request


@pytest.mark.parametrize("who", ["mo", "kofi", "rhiannon", "asha"])
def test_only_system_admins_read_the_trail(web: Web, who: str):
    client = as_(web, who)
    for path in ("events", "rbac", "usage", "checks"):
        assert client.get(f"/admin/audit/{path}").status_code == 403


def test_rbac_snapshot_lists_every_role_holder_without_contact_details(web: Web):
    snapshot = as_(web, "sam").get("/admin/audit/rbac").json()
    assert {(r["display_name"], r["role"]) for r in snapshot["system_roles"]} == {
        ("Sam", "admin"), ("Mo", "moderator"),
    }  # fmt: skip
    by_org = {o["name"]: o for o in snapshot["organisations"]}
    assert [(r["display_name"], r["role"]) for r in by_org["Cyber Scallywags"]["roles"]] == [
        ("Kofi", "org_admin")
    ]
    assert by_org["InterLinkRCT"]["roles"] == [] and by_org["DataKind UK"]["volunteers"] == 5
    assert "@" not in json.dumps(snapshot)


def test_usage_summary_counts_outcomes_and_who_was_refused(web: Web):
    asha = as_(web, "asha")
    for _ in range(2):
        asha.get("/admin/users")
    asha.put("/me/active-org", {"org_id": CS})
    usage = as_(web, "sam").get("/admin/audit/usage", params={"days": 1}).json()
    assert usage["outcomes"]["denied"] == 2 and usage["outcomes"]["ok"] >= 2  # + sign-ins
    assert usage["most_refused"] == [
        {"actor_id": USERS["asha"], "display_name": "Asha", "refusals": 2}
    ]
    assert {"action": "http.get", "outcome": "denied", "events": 2} in usage["actions"]
    assert usage["agents"] == []


def check_ids(web: Web) -> dict[str, str]:
    found = as_(web, "sam").get("/admin/audit/checks").json()
    return {c["id"]: c["severity"] for c in found}


def test_checks_on_the_demo_seed(web: Web):
    ids = check_ids(web)
    assert ids.pop("single-system-admin") == "medium"
    [(no_admin, severity)] = ids.items()  # InterLinkRCT
    assert no_admin.startswith("org-without-admin:") and severity == "medium"


def test_a_suspended_account_that_keeps_a_role_is_a_high_finding(web: Web):
    sam, mo = as_(web, "sam"), as_(web, "mo")
    sam.put(f"/admin/orgs/{DK}/roles/{USERS['dev']}/reviewer")
    with web.rt.db.read() as conn:
        version = conn.execute(
            "SELECT status_version FROM users WHERE id = ?", (USERS["dev"],)
        ).fetchone()[0]
    r = mo.post(f"/moderation/volunteers/{USERS['dev']}/decision",
                {"action": "suspend", "note": "Under review", "version": version})  # fmt: skip
    assert r.status_code == 200, r.text
    found = as_(web, "sam").get("/admin/audit/checks").json()
    assert found[0]["severity"] == "high"  # most severe first
    assert found[0]["id"] == f"role-held-while-suspended:{USERS['dev']}:reviewer"
    assert found[0]["evidence"] == "Dev is suspended but is still reviewer for DataKind UK."


def test_repeated_refusals_and_self_granted_roles_are_findings(web: Web):
    asha, sam = as_(web, "asha"), as_(web, "sam")
    for _ in range(audit.DENIAL_BURST):
        asha.get("/admin/users")
    sam.put(f"/admin/users/{SAM}/system-roles/moderator")
    ids = check_ids(web)
    assert ids[f"repeated-refusals:{USERS['asha']}"] == "medium"
    assert ids[f"self-granted:{SAM}:moderator"] == "medium"


def test_many_failed_sign_ins_are_a_finding(web: Web):
    for _ in range(audit.FAILED_SIGN_IN_BURST):
        web.client.post("/api/v1/session", json={"email": "sam@cyberscallywags.uk",
                                                 "password": "guess-guess-guess"})  # fmt: skip
    assert check_ids(web)["failed-sign-ins"] == "medium"


def test_an_organisation_with_suggestions_waiting_and_no_admin_is_high(web: Web):
    assert as_(web, "sam").delete(f"/admin/orgs/{DK}/roles/{USERS['rhiannon']}/org_admin")
    assert check_ids(web)[f"org-without-admin:{DK}"] == "high"  # Outreach Data Audit is waiting


def test_old_audit_rows_and_abandoned_runs_are_pruned(web: Web):
    old = days_ago(500)
    with web.rt.db.tx() as conn:
        audit.record(conn, "http.post", actor_id=MO)
        conn.execute("UPDATE audit_log SET created_at = ?", (old,))
        audit.record(conn, "http.post", actor_id=MO)
        for run_id, created in (("run_waiting", days_ago(3)), ("run_fresh", days_ago(0))):
            conn.execute(
                "INSERT INTO agent_runs (id, agent, actor_id, status, messages, pending, "
                "created_at) VALUES (?, 'audit', ?, 'awaiting_approval', '[]', '[1]', ?)",
                (run_id, SAM, created),
            )
        assert prune(conn) == 0  # the routine prune leaves the trail alone
        assert prune(conn, audit_days=400) == 1
        runs = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM agent_runs")}
        assert conn.execute("SELECT count(*) FROM audit_log").fetchone()[0] == 1
    assert runs["run_fresh"]["status"] == "awaiting_approval"
    abandoned = runs["run_waiting"]
    assert abandoned["status"] == "failed" and abandoned["messages"] is None
    assert abandoned["error"] == "approval not given in time" and abandoned["pending"] == "[]"


def test_admin_reads_refuse_everyone_else_at_the_service_too(rt):
    for read in (audit.list_events, audit.rbac_snapshot, audit.usage_summary, audit.checks):
        with pytest.raises(services.Forbidden):
            read(rt.db, MO)


def test_audit_command_prints_the_checks(tmp_path, monkeypatch, capsys):
    settings = Settings(data_dir=tmp_path, _env_file=None)
    reset_demo(settings)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    assert cli.main(["audit"]) == 0  # nothing high in the demo seed
    out = capsys.readouterr().out
    assert "single-system-admin" in out and "InterLinkRCT has no org admin" in out
