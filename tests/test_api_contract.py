"""The JSON API's contract. Every route declares its response, every route is called here so a
response that does not fit its model fails, and the mobile app's hand-written types are checked
against the OpenAPI document so the two cannot drift apart."""

import json
import re
from pathlib import Path

import pytest

from app import cli
from app.runtime import DEMO_PASSWORD
from app.web import api
from tests.conftest import AUDIT, CS, DK, FOOD_BANK, USERS, WINTER, Web
from tests.test_rbac_onboarding import NIA, as_, full_profile, placement, status_version
from tests.test_submissions import DRAFT

MOBILE_TYPES = Path(__file__).parents[1] / "mobile/src/app/core/api.types.ts"
# Mobile type name -> API schema name, where the app uses a shorter name.
RENAMED = {
    "Placement": "MyPlacement",
    "Submission": "SubmissionSummary",
    "ProfileUpdate": "ProfileIn",
}
UNIONS = {
    "VolunteerStatus": ("Me", "status"),
    "SkillState": ("ProfileSkill", "state"),
    "CauseStance": ("ProfileCause", "stance"),
}


@pytest.fixture
def schema(web: Web) -> dict:
    return web.get("/api/openapi.json").json()


def _resolve(schema: dict, node: dict) -> dict:
    while "$ref" in node:
        node = schema["components"]["schemas"][node["$ref"].rsplit("/", 1)[1]]
    return node


def test_every_route_declares_a_response_model(schema: dict):
    untyped = []
    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            for code, response in operation["responses"].items():
                if not code.startswith("2") or code == "204":
                    continue
                body = response["content"]["application/json"]["schema"]
                item = body.get("items", body)
                if "$ref" not in item:
                    untyped.append(f"{method.upper()} {path}")
    assert untyped == []
    assert len(api.router.routes) == 50  # a new route must be added to the calls below


def test_every_route_answers_in_the_shape_it_declares(web: Web):
    """A response that does not fit its model is a 500, so 2xx everywhere proves the models."""
    called: set[tuple[str, str]] = set()

    def ok(response, method: str, route: str, status: int = 200):
        assert response.status_code == status, (method, route, response.text)
        called.add((method, route))
        return response.json() if response.content else None

    r = web.client.post(
        "/api/v1/auth/signup",
        json={"email": "ffion@cyberscallywags.uk", "display_name": "Ffion",
              "password": DEMO_PASSWORD},
    )  # fmt: skip
    ffion_id = ok(r, "POST", "/auth/signup", 201)["user_id"]
    ffion, asha, rhiannon = as_(web, "ffion"), as_(web, "asha"), as_(web, "rhiannon")
    sam, mo, dev, bryn = as_(web, "sam"), as_(web, "mo"), as_(web, "dev"), as_(web, "bryn")
    called.add(("POST", "/session"))

    # ---- me
    me = ok(asha.get("/me"), "GET", "/me")
    assert {o["key"] for o in me["organisations"]} == {
        "datakind-uk",
        "cyber-scallywags",
        "interlinkrct",
    }
    ok(asha.put("/me/active-org", {"org_id": CS}), "PUT", "/me/active-org")
    profile = ok(asha.get("/profile"), "GET", "/profile")
    assert list(profile["skill_states"]) == ["none", "has", "offers", "exclude"]
    assert [g["category"] for g in profile["skill_groups"]][0] == profile["skills"][0]["category"]
    assert all(p["state_label"] for p in profile["projects"]["active"])
    ok(ffion.put(f"/orgs/{CS}/membership"), "PUT", "/orgs/{org_id}/membership")
    ok(ffion.put("/profile", full_profile(ffion, **{"first-aid": "offers"})), "PUT", "/profile")
    ok(ffion.post("/me/vetting"), "POST", "/me/vetting")
    notes = ok(asha.get("/notifications"), "GET", "/notifications")
    ok(asha.post(f"/notifications/{notes[0]['id']}/read"), "POST",
       "/notifications/{notification_id}/read")  # fmt: skip
    ok(asha.post("/notifications/read-all"), "POST", "/notifications/read-all")

    # ---- organisations
    ok(asha.get("/orgs"), "GET", "/orgs")
    ok(asha.get(f"/orgs/{CS}/matches"), "GET", "/orgs/{org_id}/matches")
    ok(asha.get(f"/orgs/{DK}/projects"), "GET", "/orgs/{org_id}/projects")
    ok(rhiannon.get(f"/orgs/{DK}/coverage"), "GET", "/orgs/{org_id}/coverage")
    people = ok(rhiannon.get(f"/orgs/{DK}/volunteers"), "GET", "/orgs/{org_id}/volunteers")
    assert set(people[0]) == {"id", "display_name", "status", "joined_at", "offers", "teams"}

    # ---- moderation
    ok(mo.get("/moderation/volunteers"), "GET", "/moderation/volunteers")
    record = ok(mo.get(f"/moderation/volunteers/{NIA}"), "GET", "/moderation/volunteers/{user_id}")
    assert "email" not in record and record["vetted_at"] is None  # omitted, not null
    assert sam.get(f"/moderation/volunteers/{NIA}").json()["email"] == "nia@cyberscallywags.uk"
    ok(mo.post(f"/moderation/volunteers/{ffion_id}/decision",
               {"action": "approve", "version": status_version(web, ffion_id)}),
       "POST", "/moderation/volunteers/{user_id}/decision")  # fmt: skip

    # ---- project onboarding
    draft = ok(asha.post("/submissions/drafts", {"title": "Half an idea", "org_id": DK}),
               "POST", "/submissions/drafts", 201)  # fmt: skip
    assert draft["state"] == "draft"
    detail = asha.get(f"/submissions/{draft['id']}").json()
    assert detail["can_edit"] and detail["current"]["cause_id"] is None
    assert rhiannon.get(f"/submissions/{draft['id']}").status_code == 404
    assert asha.post(f"/submissions/{draft['id']}/submit", {"version": 1}).status_code == 422
    ok(asha.put(f"/submissions/{draft['id']}/draft", {**DRAFT, "org_id": DK, "version": 1}),
       "PUT", "/submissions/{sid}/draft")  # fmt: skip
    assert ok(asha.post(f"/submissions/{draft['id']}/submit", {"version": 2}),
              "POST", "/submissions/{sid}/submit") == {"state": "submitted"}  # fmt: skip
    scrap = asha.post("/submissions/drafts", {"title": "Scrap this", "org_id": DK}).json()
    ok(asha.delete(f"/submissions/{scrap['id']}?version=1"), "DELETE", "/submissions/{sid}")
    assert asha.get(f"/submissions/{scrap['id']}").status_code == 404
    created = ok(asha.post("/submissions", {**DRAFT, "org_id": DK}), "POST", "/submissions", 201)
    mine = ok(asha.get("/submissions"), "GET", "/submissions")
    assert mine[0]["state_label"] == "In review"
    queue = ok(rhiannon.get(f"/orgs/{DK}/submissions"), "GET", "/orgs/{org_id}/submissions")
    assert created["id"] in [q["id"] for q in queue]
    detail = ok(rhiannon.get(f"/submissions/{created['id']}"), "GET", "/submissions/{sid}")
    assert "approved_by" not in detail and "withdrawn_by" not in detail  # names, never raw IDs
    decision = {"action": "changes_requested", "reason": "Say more", "revision": 1,
                "version": detail["version"]}  # fmt: skip
    ok(rhiannon.post(f"/orgs/{DK}/submissions/{created['id']}/decision", decision), "POST",
       "/orgs/{org_id}/submissions/{sid}/decision")  # fmt: skip
    version = asha.get(f"/submissions/{created['id']}").json()["version"]
    ok(asha.post(f"/submissions/{created['id']}/revisions", {**DRAFT, "version": version}),
       "POST", "/submissions/{sid}/revisions")  # fmt: skip
    version = rhiannon.get(f"/submissions/{AUDIT}").json()["version"]
    assert rhiannon.post(
        f"/orgs/{DK}/submissions/{AUDIT}/decision",
        {"action": "approved", "reason": "Good to go", "revision": 1, "version": version},
    ).json() == {"state": "published"}
    ok(rhiannon.put(f"/orgs/{DK}/projects/{AUDIT}/lead", {"user_id": USERS["dev"]}), "PUT",
       "/orgs/{org_id}/projects/{sid}/lead")  # fmt: skip
    version = rhiannon.get(f"/submissions/{AUDIT}").json()["version"]
    ok(rhiannon.post(f"/orgs/{DK}/projects/{AUDIT}/withdraw",
                     {"reason": "Superseded", "version": version}),
       "POST", "/orgs/{org_id}/projects/{sid}/withdraw")  # fmt: skip

    # ---- placements
    team = ok(rhiannon.get(f"/projects/{FOOD_BANK}/placements"), "GET",
              "/projects/{project_id}/placements")  # fmt: skip
    assert "display_name" in team[0] and "email" not in str(team)
    version = placement(web, FOOD_BANK, USERS["dev"])["version"]
    ok(rhiannon.post(f"/projects/{FOOD_BANK}/placements/{USERS['dev']}/answer",
                     {"answer": "yes", "version": version}),
       "POST", "/projects/{project_id}/placements/{volunteer_id}/answer")  # fmt: skip
    offers = ok(dev.get("/placements"), "GET", "/placements")
    assert {"title", "org_id", "org_name", "state_label"} <= set(offers[0])
    version = placement(web, FOOD_BANK, USERS["dev"])["version"]
    ok(dev.post(f"/placements/{FOOD_BANK}/answer", {"answer": "yes", "version": version}),
       "POST", "/placements/{project_id}/answer")  # fmt: skip
    ok(rhiannon.post(f"/projects/{FOOD_BANK}/placements/{USERS['dev']}/remove",
                     {"reason": "Rota is full"}),
       "POST", "/projects/{project_id}/placements/{volunteer_id}/remove")  # fmt: skip
    ok(rhiannon.post(f"/projects/{FOOD_BANK}/placements/{USERS['dev']}/reopen"),
       "POST", "/projects/{project_id}/placements/{volunteer_id}/reopen")  # fmt: skip
    ok(bryn.post(f"/placements/{WINTER}/leave"), "POST", "/placements/{project_id}/leave")
    ok(rhiannon.post(f"/orgs/{DK}/volunteers/{USERS['cerys']}/remove", {"reason": "Moved away"}),
       "POST", "/orgs/{org_id}/volunteers/{user_id}/remove")  # fmt: skip
    ok(ffion.delete(f"/orgs/{CS}/membership"), "DELETE", "/orgs/{org_id}/membership")

    # ---- system admin
    users = ok(sam.get("/admin/users"), "GET", "/admin/users")
    assert {u["email"] for u in users} >= {"ffion@cyberscallywags.uk", "asha@cyberscallywags.uk"}
    gwen = USERS["gwen"]
    ok(sam.put(f"/admin/users/{gwen}/system-roles/moderator"), "PUT",
       "/admin/users/{user_id}/system-roles/{role}")  # fmt: skip
    ok(sam.delete(f"/admin/users/{gwen}/system-roles/moderator"), "DELETE",
       "/admin/users/{user_id}/system-roles/{role}")  # fmt: skip
    ok(sam.put(f"/admin/orgs/{DK}/roles/{gwen}/reviewer"), "PUT",
       "/admin/orgs/{org_id}/roles/{user_id}/{role}")  # fmt: skip
    ok(sam.delete(f"/admin/orgs/{DK}/roles/{gwen}/reviewer"), "DELETE",
       "/admin/orgs/{org_id}/roles/{user_id}/{role}")  # fmt: skip
    ok(sam.post("/admin/orgs", {"key": "new-org", "name": "New Org"}), "POST", "/admin/orgs", 201)
    ok(sam.get("/admin/projection"), "GET", "/admin/projection")
    ok(sam.post("/admin/projection/replay"), "POST", "/admin/projection/replay")
    graph = ok(sam.get("/admin/graph"), "GET", "/admin/graph")
    assert {r["type"] for r in graph["relationships"]} >= {"OWNS_PROJECT", "VOLUNTEERS_WITH"}
    ok(asha.delete("/session"), "DELETE", "/session", 204)

    declared = {
        (method, route.path.removeprefix("/api/v1"))
        for route in api.router.routes
        for method in route.methods
    }
    assert declared - called == set()


def test_openapi_command_writes_the_document_the_app_serves(schema: dict, tmp_path, capsys):
    out = tmp_path / "openapi.json"
    assert cli.main(["openapi", "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8")) == schema
    capsys.readouterr()
    assert cli.main(["openapi"]) == 0
    assert json.loads(capsys.readouterr().out) == schema


# ------------------------------------------------------------------ the mobile app's types
def _interfaces(source: str) -> dict[str, list[str]]:
    """Top-level field names of each `export interface` in a TypeScript source."""
    found = {}
    for name, body in re.findall(r"export interface (\w+) \{\n(.*?)\n\}", source, re.S):
        found[name] = re.findall(r"^  (\w+)\??:", body, re.M)
    return found


def _union(source: str, name: str) -> set[str]:
    declared = re.search(rf"export type {name} = ([^;]+);", source).group(1)
    return set(re.findall(r"'([^']+)'", declared))


def test_mobile_types_use_only_fields_the_api_declares(schema: dict):
    source = MOBILE_TYPES.read_text(encoding="utf-8")
    interfaces = _interfaces(source)
    assert len(interfaces) >= 12
    components = schema["components"]["schemas"]
    problems = []
    for name, fields in interfaces.items():
        model = components.get(RENAMED.get(name, name))
        if model is None:
            problems.append(f"{name}: no such API model")
            continue
        unknown = set(fields) - set(model["properties"])
        if unknown:
            problems.append(f"{name}: {sorted(unknown)} not in the API model")
    assert problems == []


def test_mobile_state_unions_match_the_api(schema: dict):
    source = MOBILE_TYPES.read_text(encoding="utf-8")
    components = schema["components"]["schemas"]
    for name, (model, field) in UNIONS.items():
        declared = _resolve(schema, components[model]["properties"][field])
        assert _union(source, name) == set(declared["enum"]), name
    placement_states = re.search(r"state: ('eligible'[^;]+);", source).group(1)
    assert set(re.findall(r"'([^']+)'", placement_states)) == set(
        components["MyPlacement"]["properties"]["state"]["enum"]
    )
    submission_states = re.search(r"state: ('draft'[^;]+);", source).group(1)
    assert set(re.findall(r"'([^']+)'", submission_states)) == set(
        components["SubmissionSummary"]["properties"]["state"]["enum"]
    )
