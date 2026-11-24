"""A project's own team onboarding, inside its organisation's rules: the lead the org admins
assign and the org admins run it; a volunteer who accepts an invitation goes through its steps;
the lead side then accepts them onto the team, and can suspend, reinstate or remove them."""

import pytest

from app.services import onboarding
from tests.agent_models import call, final
from tests.conftest import CS, DK, FOOD_BANK, USERS, WINTER, Web
from tests.test_onboarding import ADVICE, assistant, workflow
from tests.test_rbac_onboarding import Client, as_, graph_edges, placement

BASE = f"/orgs/{DK}/projects/{FOOD_BANK}/onboarding"
TEAM = f"/projects/{FOOD_BANK}/placements"
DEV = USERS["dev"]
BRIEF, CALL = "Read the project brief", "Intro call with the project lead"


@pytest.fixture(autouse=True)
def team_onboarding_on(web: Web) -> None:
    """The demo's Food Bank project comes with its steps ready and switched off. Its lead
    switches them on."""
    cerys = as_(web, "cerys")
    seeded = flow(cerys)
    assert (seeded["enabled"], len(seeded["steps"])) == (False, 2)
    r = cerys.put(BASE, {"enabled": True, "version": seeded["version"]})
    assert r.status_code == 200, r.text


def flow(who: Client, base: str = BASE) -> dict:
    r = who.get(base)
    assert r.status_code == 200, r.text
    return r.json()


def step_ids(who: Client) -> dict[str, str]:
    return {s["title"]: s["id"] for s in flow(who)["steps"]}


def invite_and_say_yes(web: Web, lead: Client, volunteer: Client, user_id: str) -> str:
    """The lead invites, the volunteer says yes. Returns the placement's state."""
    version = placement(web, FOOD_BANK, user_id)["version"]
    r = lead.post(f"{TEAM}/{user_id}/answer", {"answer": "yes", "version": version})
    assert r.status_code == 200, r.text
    r = volunteer.post(f"/placements/{FOOD_BANK}/answer", {"answer": "yes", "version": version + 1})
    assert r.status_code == 200, r.text
    return r.json()["state"]


def onto_the_team(web: Web) -> tuple[Client, Client, Client]:
    """Dev goes all the way: invited, through both steps, accepted. (lead, org admin, dev)"""
    cerys, rhiannon, dev = as_(web, "cerys"), as_(web, "rhiannon"), as_(web, "dev")
    assert invite_and_say_yes(web, cerys, dev, DEV) == "onboarding"
    steps = step_ids(cerys)
    assert dev.post(f"{BASE}/steps/{steps[BRIEF]}/complete").status_code == 200
    assert cerys.post(f"{BASE}/volunteers/{DEV}/steps/{steps[CALL]}/complete").status_code == 200
    assert cerys.post(f"{TEAM}/{DEV}/accept").json() == {"state": "team"}
    return cerys, rhiannon, dev


def project_steps(web: Web, user_id: str) -> list[tuple[str, str]]:
    """(relationship, step title) for the volunteer's place in projects' workflows, in the graph."""
    rows = web.rt.graph.read(
        "MATCH (:Volunteer {id: $id})-[r:AT_STEP|COMPLETED_STEP]->(s:OnboardingStep)"
        "<-[:HAS_TEAM_STEP]-(:Project) RETURN type(r) AS rel, s.title AS title ORDER BY s.position",
        {"id": user_id},
    )
    return [(r["rel"], r["title"]) for r in rows]


# ------------------------------------------------------------------ who runs it
def test_the_demo_project_has_its_own_team_onboarding_held_in_the_graph(web: Web):
    found = flow(as_(web, "cerys"))
    assert found["enabled"] is True
    assert [(s["title"], s["completed_by"]) for s in found["steps"]] == [
        (BRIEF, "volunteer"), (CALL, "lead"),
    ]  # fmt: skip
    assert [c["value"] for c in found["completers"]] == ["volunteer", "lead"]
    assert {t["name"] for t in found["templates"]} >= {"Welcome", "Next step"}  # DataKind UK's
    rows = web.rt.graph.read(
        "MATCH (p:Project {id: $id})-[:HAS_TEAM_STEP]->(s:OnboardingStep) "
        "OPTIONAL MATCH (s)-[:NEXT_STEP]->(n) "
        "RETURN p.onboarding_enabled AS enabled, s.title AS title, n.title AS next "
        "ORDER BY s.position",
        {"id": FOOD_BANK},
    )
    assert rows == [
        {"enabled": True, "title": BRIEF, "next": CALL},
        {"enabled": True, "title": CALL, "next": None},
    ]
    # a project with nothing set up reads as empty and switched off
    empty = flow(as_(web, "asha"), f"/orgs/{CS}/projects/{WINTER}/onboarding")
    assert (empty["enabled"], empty["steps"], empty["version"]) == (False, [], 0)


def test_only_the_lead_and_the_org_admins_run_a_projects_workflow(web: Web):
    cerys, rhiannon = as_(web, "cerys"), as_(web, "rhiannon")
    for who in (cerys, rhiannon, as_(web, "sam")):  # the lead, an org admin, a system admin
        assert who.get(BASE).status_code == 200
        assert who.get(f"{BASE}/volunteers").status_code == 200
    dev, bryn = as_(web, "dev"), as_(web, "bryn")
    new = {"title": "Sign the data agreement", "completed_by": "volunteer", "version": 1}
    assert dev.get(BASE).status_code == 403  # a volunteer of the organisation, not the lead
    assert dev.post(f"{BASE}/steps", new).status_code == 403
    assert bryn.get(BASE).status_code == 404  # not with the organisation at all
    assert as_(web, "kofi").get(f"{BASE}/volunteers").status_code == 404  # admin elsewhere
    # the project belongs to its organisation: another organisation's URL does not reach it
    assert rhiannon.get(f"/orgs/{CS}/projects/{FOOD_BANK}/onboarding").status_code in (403, 404)
    assert as_(web, "sam").get(f"/orgs/{CS}/projects/{FOOD_BANK}/onboarding").status_code == 404
    # the lead edits the steps, and a stale version is refused
    version = flow(cerys)["version"]
    r = cerys.post(f"{BASE}/steps", new | {"version": version})
    assert r.status_code == 201 and [s["title"] for s in r.json()["steps"]][-1] == new["title"]
    assert cerys.post(f"{BASE}/steps", new | {"version": version}).status_code == 409


def test_a_projects_workflow_stays_inside_the_organisations_rules(web: Web):
    cerys, rhiannon = as_(web, "cerys"), as_(web, "rhiannon")
    found = flow(cerys)
    step = {"title": "Sign the data agreement", "completed_by": "volunteer"}
    # a project's step is confirmed by its lead side, never "an organisation admin" as such
    r = cerys.post(f"{BASE}/steps", step | {"completed_by": "org_admin", "version": 1})
    assert r.status_code == 422 and "project lead" in r.json()["detail"]
    r = rhiannon.post(
        f"/orgs/{DK}/onboarding/steps",
        step | {"completed_by": "lead", "version": workflow(rhiannon, DK)["version"]},
    )
    assert r.status_code == 422  # and the organisation's own workflow has no lead
    # its emails are the organisation's templates: not another organisation's
    other = workflow(as_(web, "kofi"), CS)["templates"][0]["id"]
    r = cerys.post(
        f"{BASE}/steps", step | {"email_template_id": other, "version": found["version"]}
    )
    assert r.status_code == 422 and "this organisation's email templates" in r.json()["detail"]
    # and the lead cannot write templates, or touch the organisation's own workflow
    template = {"name": "Mine", "subject": "Hello there", "body": "A body long enough.",
                "version": 1}  # fmt: skip
    assert cerys.post(f"/orgs/{DK}/onboarding/templates", template).status_code == 403
    assert cerys.get(f"/orgs/{DK}/onboarding").status_code == 403


def test_only_org_admins_assign_the_lead_and_the_powers_move_with_it(web: Web):
    cerys, rhiannon, dev = as_(web, "cerys"), as_(web, "rhiannon"), as_(web, "dev")
    lead = f"/orgs/{DK}/projects/{FOOD_BANK}/lead"
    assert cerys.put(lead, {"user_id": DEV}).status_code == 403  # a lead cannot hand it on
    assert dev.put(lead, {"user_id": DEV}).status_code == 403
    assert rhiannon.put(lead, {"user_id": USERS["bryn"]}).status_code == 422  # not with DataKind
    assert rhiannon.put(lead, {"user_id": DEV}).json() == {"lead_id": DEV}
    assert dev.get(BASE).status_code == 200 and dev.get(TEAM).status_code == 200
    assert cerys.get(BASE).status_code == 403 and cerys.get(TEAM).status_code == 403
    assert cerys.post(f"{TEAM}/{USERS['kofi']}/suspend", {"reason": "Away"}).status_code == 403


# ------------------------------------------------------------------ joining a team
def test_a_volunteer_goes_through_the_projects_steps_and_is_then_accepted(web: Web):
    cerys, dev = as_(web, "cerys"), as_(web, "dev")
    assert invite_and_say_yes(web, cerys, dev, DEV) == "onboarding"
    assert FOOD_BANK not in graph_edges(web, DEV, "ON_TEAM")  # not on the team yet
    assert project_steps(web, DEV) == [("AT_STEP", BRIEF)]
    assert "“Food Bank Demand Dashboard” onboarding, step 1 of 2: Read the project brief." in (
        dev.messages()
    )
    assert any("has started its onboarding steps" in m for m in cerys.messages())
    [run] = dev.get("/me/onboarding").json()
    assert (run["project_id"], run["project"]) == (FOOD_BANK, "Food Bank Demand Dashboard")
    assert (run["state"], run["steps_done"], run["awaiting_acceptance"]) == (
        "in_progress",
        0,
        False,
    )
    brief, intro = (s["id"] for s in run["steps"])
    mine = next(p for p in cerys.get(TEAM).json() if p["volunteer_id"] == DEV)
    assert (mine["state"], mine["steps_left"]) == ("onboarding", 2)

    # nobody is accepted with steps still open, and nobody accepts themselves
    r = cerys.post(f"{TEAM}/{DEV}/accept")
    assert r.status_code == 422 and "still has 2 onboarding steps" in r.json()["detail"]
    assert dev.post(f"{TEAM}/{DEV}/accept").status_code == 403
    # steps in order; the lead's step is not the volunteer's to tick off
    assert dev.post(f"{BASE}/steps/{intro}/complete").status_code == 403
    assert dev.post(f"{BASE}/steps/{brief}/complete").json()["steps_done"] == 1
    assert dev.post(f"{BASE}/steps/{intro}/complete").status_code == 403
    assert dev.post(f"{BASE}/volunteers/{DEV}/steps/{intro}/complete").status_code == 403
    [waiting] = cerys.get(f"{BASE}/volunteers").json()
    assert (waiting["current_step"], waiting["waiting_for"]) == (CALL, "lead")
    done = cerys.post(f"{BASE}/volunteers/{DEV}/steps/{intro}/complete").json()
    assert (done["state"], done["awaiting_acceptance"]) == ("completed", True)
    assert done["state_label"] == onboarding.AWAITING_ACCEPTANCE

    # finishing the steps puts nobody on the team: the lead side accepts
    assert placement(web, FOOD_BANK, DEV)["state"] == "onboarding"
    assert FOOD_BANK not in graph_edges(web, DEV, "ON_TEAM")
    assert project_steps(web, DEV) == [("COMPLETED_STEP", BRIEF), ("COMPLETED_STEP", CALL)]
    assert any("Its lead will now confirm your place" in m for m in dev.messages())
    assert any("Accept them onto the team" in m for m in cerys.messages())
    assert cerys.post(f"{TEAM}/{DEV}/accept").json() == {"state": "team"}
    assert FOOD_BANK in graph_edges(web, DEV, "ON_TEAM")
    assert "You have been accepted onto the team for “Food Bank Demand Dashboard”." in (
        dev.messages()
    )
    assert cerys.post(f"{TEAM}/{DEV}/accept").status_code == 409  # already done
    [run] = dev.get("/me/onboarding").json()
    assert (run["state"], run["awaiting_acceptance"]) == ("completed", False)


def test_an_org_admin_can_do_everything_the_lead_can(web: Web):
    rhiannon, dev = as_(web, "rhiannon"), as_(web, "dev")
    assert invite_and_say_yes(web, rhiannon, dev, DEV) == "onboarding"
    for step in step_ids(rhiannon).values():  # an admin may tick off the volunteer's own step
        assert rhiannon.post(f"{BASE}/volunteers/{DEV}/steps/{step}/complete").status_code == 200
    assert rhiannon.post(f"{TEAM}/{DEV}/accept").json() == {"state": "team"}


def test_a_project_without_a_workflow_takes_a_volunteer_straight_onto_the_team(web: Web):
    cerys, dev = as_(web, "cerys"), as_(web, "dev")
    found = flow(cerys)
    r = cerys.put(BASE, {"enabled": False, "version": found["version"]})
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert invite_and_say_yes(web, cerys, dev, DEV) == "team"
    assert dev.get("/me/onboarding").json() == []
    assert cerys.post(f"{TEAM}/{DEV}/accept").status_code == 409  # nothing to accept


def test_the_organisations_own_onboarding_comes_first(web: Web):
    cerys, rhiannon, dev = as_(web, "cerys"), as_(web, "rhiannon"), as_(web, "dev")
    with web.rt.db.tx() as conn:  # Dev is part-way through DataKind UK's own onboarding
        conn.execute("UPDATE onboarding_workflows SET enabled = 1 WHERE org_id = ?", (DK,))
        assert onboarding.start(conn, DEV, DK)
    version = placement(web, FOOD_BANK, DEV)["version"]
    cerys.post(f"{TEAM}/{DEV}/answer", {"answer": "yes", "version": version})
    yes = {"answer": "yes", "version": version + 1}
    r = dev.post(f"/placements/{FOOD_BANK}/answer", yes)
    assert (
        r.status_code == 409
        and "Finish your onboarding with DataKind UK first" in (r.json()["detail"])
    )
    assert placement(web, FOOD_BANK, DEV)["state"] == "eligible"  # the invitation stays open
    for step in workflow(rhiannon, DK)["steps"]:
        rhiannon.post(f"/orgs/{DK}/onboarding/volunteers/{DEV}/steps/{step['id']}/complete")
    assert dev.post(f"/placements/{FOOD_BANK}/answer", yes).json() == {"state": "onboarding"}
    # the same rule holds at the moment of acceptance
    with web.rt.db.tx() as conn:
        assert onboarding.start(conn, DEV, DK)
    for step in step_ids(cerys).values():
        cerys.post(f"{BASE}/volunteers/{DEV}/steps/{step}/complete")
    r = cerys.post(f"{TEAM}/{DEV}/accept")
    assert r.status_code == 422 and "not finished onboarding with DataKind UK" in r.json()["detail"]


def test_losing_standing_with_the_organisation_ends_the_projects_onboarding(web: Web):
    cerys, dev = as_(web, "cerys"), as_(web, "dev")
    invite_and_say_yes(web, cerys, dev, DEV)
    assert dev.delete(f"/orgs/{DK}/membership").status_code == 200
    assert placement(web, FOOD_BANK, DEV)["state"] == "left"
    assert dev.get("/me/onboarding").json() == [] and project_steps(web, DEV) == []
    assert cerys.get(f"{BASE}/volunteers").json() == []
    assert cerys.post(f"{TEAM}/{DEV}/accept").status_code in (409, 422)


def test_editing_the_steps_moves_volunteers_and_emptying_them_leaves_only_acceptance(web: Web):
    cerys, dev = as_(web, "cerys"), as_(web, "dev")
    invite_and_say_yes(web, cerys, dev, DEV)
    steps = step_ids(cerys)
    version = flow(cerys)["version"]
    r = cerys.delete(f"{BASE}/steps/{steps[BRIEF]}?version={version}")
    assert [s["title"] for s in r.json()["steps"]] == [CALL]
    assert project_steps(web, DEV) == [("AT_STEP", CALL)]  # moved on, in the graph too
    r = cerys.delete(f"{BASE}/steps/{steps[CALL]}?version={r.json()['version']}")
    assert (r.json()["steps"], r.json()["enabled"]) == ([], False)
    [run] = dev.get("/me/onboarding").json()
    assert run["awaiting_acceptance"] is True  # still the lead side's decision
    assert cerys.post(f"{TEAM}/{DEV}/accept").json() == {"state": "team"}


# ------------------------------------------------------------------ suspend, reinstate, remove
def test_the_lead_side_suspends_and_reinstates_a_team_member(web: Web):
    cerys, rhiannon, dev = onto_the_team(web)
    assert dev.post(f"{TEAM}/{DEV}/suspend", {"reason": "Testing"}).status_code == 403
    assert (
        as_(web, "farah").post(f"{TEAM}/{DEV}/suspend", {"reason": "Not needed"}).status_code == 403
    )
    assert cerys.post(f"{TEAM}/{DEV}/suspend", {"reason": ""}).status_code == 422  # say why
    r = cerys.post(f"{TEAM}/{DEV}/suspend", {"reason": "Missed three sessions"})
    assert r.json() == {"state": "suspended"}
    assert FOOD_BANK not in graph_edges(web, DEV, "ON_TEAM")  # off the team in the graph
    assert "You have been suspended from the team for “Food Bank Demand Dashboard”: " \
           "Missed three sessions" in dev.messages()  # fmt: skip
    row = next(p for p in rhiannon.get(TEAM).json() if p["volunteer_id"] == DEV)
    assert (row["state_label"], row["decided_by"], row["lead_note"]) == (
        "Suspended from the team", "Cerys", "Missed three sessions",
    )  # fmt: skip
    mine = next(p for p in dev.get("/placements").json() if p["project_id"] == FOOD_BANK)
    assert mine["state"] == "suspended"
    assert cerys.post(f"{TEAM}/{DEV}/suspend", {"reason": "Again"}).status_code == 409
    # an org admin reinstates what the lead suspended
    assert rhiannon.post(f"{TEAM}/{DEV}/reinstate").json() == {"state": "team"}
    assert FOOD_BANK in graph_edges(web, DEV, "ON_TEAM")
    assert "You are back on the team for “Food Bank Demand Dashboard”." in dev.messages()
    assert rhiannon.post(f"{TEAM}/{DEV}/reinstate").status_code == 409


def test_the_lead_is_not_suspended_from_their_own_team(web: Web):
    cerys, rhiannon = as_(web, "cerys"), as_(web, "rhiannon")
    lead = USERS["cerys"]
    assert cerys.post(f"{TEAM}/{lead}/suspend", {"reason": "A break"}).status_code == 403
    r = rhiannon.post(f"{TEAM}/{lead}/suspend", {"reason": "A break"})
    assert r.status_code == 422 and "assigns a new lead" in r.json()["detail"]


def test_a_suspended_member_is_not_reinstated_without_standing(web: Web):
    cerys, rhiannon, dev = onto_the_team(web)
    cerys.post(f"{TEAM}/{DEV}/suspend", {"reason": "Missed three sessions"})
    with web.rt.db.tx() as conn:  # Dev is now part-way through the organisation's own steps
        conn.execute("UPDATE onboarding_workflows SET enabled = 1 WHERE org_id = ?", (DK,))
        assert onboarding.start(conn, DEV, DK)
    r = rhiannon.post(f"{TEAM}/{DEV}/reinstate")
    assert r.status_code == 422 and "DataKind UK" in r.json()["detail"]
    assert placement(web, FOOD_BANK, DEV)["state"] == "suspended"


def test_the_lead_side_removes_someone_onboarding_or_suspended_and_they_may_leave(web: Web):
    cerys, dev = as_(web, "cerys"), as_(web, "dev")
    invite_and_say_yes(web, cerys, dev, DEV)
    r = cerys.post(f"{TEAM}/{DEV}/remove", {"reason": "The project is full"})
    assert r.json() == {"state": "removed"}
    assert dev.get("/me/onboarding").json() == [] and project_steps(web, DEV) == []
    # reopened and invited again, they start the steps afresh
    assert cerys.post(f"{TEAM}/{DEV}/reopen").json() == {"state": "eligible"}
    assert invite_and_say_yes(web, cerys, dev, DEV) == "onboarding"
    assert dev.get("/me/onboarding").json()[0]["steps_done"] == 0
    assert dev.post(f"/placements/{FOOD_BANK}/leave").json() == {"state": "left"}  # mid-way
    assert dev.get("/me/onboarding").json() == []
    # and a suspended member can be removed
    asha = as_(web, "asha")
    version = placement(web, FOOD_BANK, USERS["asha"])["version"]
    cerys.post(f"{TEAM}/{USERS['asha']}/answer", {"answer": "yes", "version": version})
    asha.post(f"/placements/{FOOD_BANK}/answer", {"answer": "yes", "version": version + 1})
    for step in step_ids(cerys).values():
        cerys.post(f"{BASE}/volunteers/{USERS['asha']}/steps/{step}/complete")
    cerys.post(f"{TEAM}/{USERS['asha']}/accept")
    cerys.post(f"{TEAM}/{USERS['asha']}/suspend", {"reason": "Unavailable this term"})
    r = cerys.post(f"{TEAM}/{USERS['asha']}/remove", {"reason": "Unavailable this term"})
    assert r.json() == {"state": "removed"}


def test_withdrawing_the_project_ends_its_onboarding_and_takes_its_steps_out_of_the_graph(
    web: Web,
):
    cerys, rhiannon, dev = as_(web, "cerys"), as_(web, "rhiannon"), as_(web, "dev")
    invite_and_say_yes(web, cerys, dev, DEV)
    with web.rt.db.read() as conn:
        version = conn.execute(
            "SELECT version FROM submissions WHERE id = ?", (FOOD_BANK,)
        ).fetchone()[0]
    r = rhiannon.post(
        f"/orgs/{DK}/projects/{FOOD_BANK}/withdraw", {"reason": "Funding ended", "version": version}
    )
    assert r.status_code == 200, r.text
    assert placement(web, FOOD_BANK, DEV)["state"] == "removed"
    assert dev.get("/me/onboarding").json() == []
    left = web.rt.graph.read(
        "MATCH (s:OnboardingStep) WHERE s.title IN $titles RETURN count(s) AS n",
        {"titles": [BRIEF, CALL]},
    )
    assert left == [{"n": 0}]


# ------------------------------------------------------------------ the web app
def test_the_leads_project_page_runs_the_team_onboarding(web: Web):
    page = f"/o/{DK}/projects/{FOOD_BANK}"
    web.sign_in("cerys")
    html = web.get(page).text
    assert "Team onboarding" in html and BRIEF in html and "switched on" in html
    assert "Assign the project lead" not in html  # only org admins assign the lead
    assert 'data-form="lead"' not in web.get(f"{page}?assign=lead").text
    r = web.post(f"{page}/onboarding/steps", {
        "title": "Sign the data agreement", "description": "", "completed_by": "volunteer",
        "email_template_id": "", "version": str(flow(as_(web, "cerys"))["version"]),
    }, csrf_from=page)  # fmt: skip
    assert r.status_code == 303 and "msg=flow_saved" in r.headers["location"]
    assert "Sign the data agreement" in web.get(page).text
    version = placement(web, FOOD_BANK, DEV)["version"]
    web.post(f"{page}/candidates/{DEV}/answer", {"answer": "yes", "version": str(version)},
             csrf_from=page)  # fmt: skip

    web.sign_in("dev")
    web.post(f"/opportunities/{FOOD_BANK}/answer", {"answer": "yes", "version": str(version + 1)},
             csrf_from="/opportunities")  # fmt: skip
    assert "See your steps" in web.get("/opportunities").text
    html = web.get("/onboarding").text
    assert "Food Bank Demand Dashboard" in html and "Mark as done" in html
    first = step_ids(as_(web, "cerys"))[BRIEF]
    r = web.post(f"/onboarding/{DK}/steps/{first}/complete", {"project_id": FOOD_BANK},
                 csrf_from="/onboarding")  # fmt: skip
    assert r.status_code == 303
    assert web.get(page).status_code == 200 and "Team onboarding" not in web.get(page).text

    web.sign_in("cerys")
    html = web.get(page).text
    assert "2 onboarding steps left" in html and "Accept onto the team" not in html
    with web.rt.db.read() as conn:
        open_steps = [
            r[0] for r in conn.execute(
                "SELECT s.id FROM onboarding_steps s WHERE s.project_id = ? ORDER BY position",
                (FOOD_BANK,),
            )
        ][1:]  # fmt: skip
    for step in open_steps:
        r = web.post(f"{page}/onboarding/volunteers/{DEV}/steps/{step}", csrf_from=page)
        assert r.status_code == 303, r.text
    assert "Accept onto the team" in web.get(page).text
    r = web.post(f"{page}/team/{DEV}/accept", csrf_from=page)
    assert "msg=accepted" in r.headers["location"]
    r = web.post(f"{page}/team/{DEV}/suspend", {"reason": "Missed three sessions"}, csrf_from=page)
    assert "msg=suspended" in r.headers["location"]
    assert "Reinstate" in web.get(page).text
    web.sign_in("dev")
    assert "suspended your place" in web.get("/opportunities").text
    web.sign_in("cerys")
    r = web.post(f"{page}/team/{DEV}/reinstate", csrf_from=page)
    assert "msg=reinstated" in r.headers["location"]
    assert placement(web, FOOD_BANK, DEV)["state"] == "team"
    r = web.post(f"{page}/onboarding", {"version": str(flow(as_(web, 'cerys'))["version"])},
                 csrf_from=page)  # fmt: skip
    assert r.status_code == 303 and "switched off" in web.get(page).text  # unticked: off


def test_an_org_admin_assigns_the_lead_from_the_project_page(web: Web):
    page = f"/o/{DK}/projects/{FOOD_BANK}"
    web.sign_in("rhiannon")
    assert "Assign the project lead" in web.get(page).text
    picker = web.get(f"{page}?assign=lead").text
    assert 'data-form="lead"' in picker and "Dev" in picker
    r = web.post(f"{page}/lead", {"lead_id": DEV}, csrf_from=page)
    assert r.status_code == 303 and "msg=lead_set" in r.headers["location"]
    with web.rt.db.read() as conn:
        lead = conn.execute("SELECT lead_id FROM submissions WHERE id = ?", (FOOD_BANK,)).fetchone()
    assert lead[0] == DEV
    web.sign_in("cerys")  # no longer the lead
    assert web.post(f"{page}/lead", {"lead_id": USERS["cerys"]}, csrf_from=page).status_code == 403


# ------------------------------------------------------------------ the onboarding assistant
def test_the_assistant_follows_a_projects_workflow_from_the_graph(web: Web):
    cerys, dev = as_(web, "cerys"), as_(web, "dev")
    invite_and_say_yes(web, cerys, dev, DEV)
    steps = step_ids(cerys)
    run, model = assistant(
        web, dev, call("my_onboarding"),
        call("complete_onboarding_step", org_id=DK, step_id=steps[CALL]),  # the lead's step
        call("complete_onboarding_step", org_id=DK, step_id=steps[BRIEF]), final(**ADVICE),
    )  # fmt: skip
    mine, refused = model.tool_results()[:2]
    assert f"project_id='{FOOD_BANK}'" in mine and "project='Food Bank Demand Dashboard'" in mine
    assert "completed_by='lead'" in mine and "awaiting_acceptance=False" in mine
    assert "not the volunteer's current step" in refused  # steps are taken in order
    assert run["status"] == "awaiting_approval"
    assert run["pending"][0]["summary"] == f"Mark as done: {BRIEF}"
    done = dev.post(f"/agents/runs/{run['run_id']}/approval", {"approve": True}).json()
    assert done["status"] == "completed"
    assert "Done. 1 of 2 steps are finished." in model.tool_results()

    # the lead's step is never the assistant's to complete, and it cannot accept anyone
    _run, model = assistant(
        web, dev, call("complete_onboarding_step", org_id=DK, step_id=steps[CALL]),
        final(**ADVICE),
    )  # fmt: skip
    assert "confirmed by the project lead, not by the volunteer" in model.tool_results()[0]
    assert not {t for t in model.tools_offered if "accept" in t or "suspend" in t}
    cerys.post(f"{BASE}/volunteers/{DEV}/steps/{steps[CALL]}/complete")
    _run, model = assistant(web, dev, call("my_onboarding"), final(**ADVICE))
    assert "finished=True" in model.tool_results()[0]
    assert "on_team=False" in model.tool_results()[0]
    assert "awaiting_acceptance=True" in model.tool_results()[0]
    cerys.post(f"{TEAM}/{DEV}/accept")
    _run, model = assistant(web, dev, call("my_onboarding"), final(**ADVICE))
    assert "on_team=True" in model.tool_results()[0]
    assert "awaiting_acceptance=False" in model.tool_results()[0]


# ------------------------------------------------------------------ team coordinators
def test_a_team_coordinator_decides_on_team_places_and_assigns_the_lead(web: Web):
    """The organisation's team coordinator does what the lead side does, for every project,
    and chooses the lead. They do not review, and they do not manage the volunteers."""
    gwen_id = USERS["gwen"]  # not a DataKind UK volunteer, and no role there yet
    sam, gwen, dev = as_(web, "sam"), as_(web, "gwen"), as_(web, "dev")
    lead = f"/orgs/{DK}/projects/{FOOD_BANK}/lead"
    assert gwen.get(TEAM).status_code == 404  # no standing: the project is not visible
    role = f"/admin/orgs/{DK}/roles/{gwen_id}/team_coordinator"
    assert sam.put(role).json() == {"granted": "team_coordinator"}
    assert "You were given the team coordinator role for DataKind UK." in gwen.messages()
    mine = next(o for o in gwen.get("/orgs").json() if o["id"] == DK)
    assert (mine["team_coordinator"], mine["reviewer"], mine["org_admin"]) == (True, False, False)

    # accepts a volunteer onto a team: invites, confirms the lead's step, accepts
    assert invite_and_say_yes(web, gwen, dev, DEV) == "onboarding"
    steps = step_ids(gwen)
    assert dev.post(f"{BASE}/steps/{steps[BRIEF]}/complete").status_code == 200
    assert gwen.post(f"{BASE}/volunteers/{DEV}/steps/{steps[CALL]}/complete").status_code == 200
    assert gwen.post(f"{TEAM}/{DEV}/accept").json() == {"state": "team"}
    assert FOOD_BANK in graph_edges(web, DEV, "ON_TEAM")

    # selects the lead, from the organisation's volunteers
    assert DEV in {p["id"] for p in gwen.get(f"/orgs/{DK}/volunteers").json()}
    assert gwen.put(lead, {"user_id": DEV}).json() == {"lead_id": DEV}
    assert any(m.startswith("You are now the lead for “Food Bank") for m in dev.messages())

    # reviewing stays with the reviewers, and managing volunteers with the org admins
    assert gwen.get(f"/orgs/{DK}/coverage").status_code == 403
    assert gwen.get(f"/orgs/{DK}/submissions").status_code == 403
    r = gwen.post(f"/orgs/{DK}/volunteers/{DEV}/remove", {"reason": "Not for me to do"})
    assert r.status_code == 403
    assert gwen.get(f"/orgs/{CS}/volunteers").status_code == 403  # one organisation only

    assert sam.delete(role).json() == {"revoked": "team_coordinator"}
    assert gwen.put(lead, {"user_id": USERS["cerys"]}).status_code in (403, 404)


def test_a_reviewer_is_not_a_team_coordinator(web: Web):
    gwen_id = USERS["gwen"]
    sam, gwen = as_(web, "sam"), as_(web, "gwen")
    assert sam.put(f"/admin/orgs/{DK}/roles/{gwen_id}/reviewer").status_code == 200
    assert gwen.get(f"/orgs/{DK}/coverage").status_code == 200  # still works on the projects
    assert gwen.get(TEAM).status_code == 403
    assert gwen.put(f"/orgs/{DK}/projects/{FOOD_BANK}/lead", {"user_id": DEV}).status_code == 403


def test_a_team_coordinators_pages(web: Web):
    with web.rt.db.tx() as conn:
        conn.execute(
            "INSERT INTO org_roles (user_id, org_id, role, granted_at) "
            "VALUES (?, ?, 'team_coordinator', ?)",
            (USERS["gwen"], DK, "2026-01-01T00:00:00+00:00"),
        )
    page = f"/o/{DK}/projects/{FOOD_BANK}"
    web.sign_in("gwen")
    home = web.get("/home").text
    assert "Team coordinator" in home and f"/o/{DK}/review" not in home
    html = web.get(page).text
    assert "Assign the project lead" in html and "Candidates and team" in html
    assert "reviewers only" not in html
    r = web.post(f"{page}/lead", {"lead_id": DEV}, csrf_from=page)
    assert r.status_code == 303 and "msg=lead_set" in r.headers["location"]
    assert web.get(f"/o/{DK}/volunteers").status_code == 403
    assert web.get(f"/o/{DK}/review").status_code == 403
