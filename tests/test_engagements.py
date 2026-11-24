"""Participation history: engagements follow the team, a completed project stays as history,
the lead side rates it on one scale, and another organisation sees a summary only with the
volunteer's consent."""

import pytest

from app import matching, services
from app.catalogue import stable_id
from app.graph import cypher
from app.services import engagements
from tests.conftest import CS, DK, FOOD_BANK, USERS, WINTER, Web
from tests.test_matching import candidates, save_profile
from tests.test_placement_rules import accept, invite
from tests.test_rbac_onboarding import as_, graph_edges, placement

ROTA = stable_id("p", "shelter-meal-rota")  # completed, Cyber Scallywags, homelessness
SURVEY = stable_id("p", "food-parcel-survey")  # completed, DataKind UK, food security


def history(web: Web, key: str) -> list[dict]:
    return engagements.my_history(web.rt.db, USERS[key])["engagements"]


def version(web: Web, project: str) -> int:
    with web.rt.db.read() as conn:
        return conn.execute("SELECT version FROM submissions WHERE id = ?", (project,)).fetchone()[
            0
        ]


def engagement(web: Web, project: str, key: str) -> dict:
    return next(e for e in history(web, key) if e["project_id"] == project)


def experience(web: Web, org: str, project: str, reviewer: str) -> dict[str, dict]:
    cov = matching.project_coverage(web.rt.db, web.rt.graph, USERS[reviewer], org, project)
    return {c["name"]: c["experience"] for c in cov.projects[0]["candidates"]}


def graph_engagement(web: Web, engagement_id: str) -> list[dict]:
    return web.rt.graph.read(
        "MATCH (v:Volunteer)-[:HAD_ENGAGEMENT]->(e:Engagement {id: $id})"
        "-[:ON_PROJECT]->(p:Project) "
        "RETURN v.id AS volunteer, p.id AS project, p.status AS status, e.role AS role, "
        "e.outcome AS outcome, e.readiness AS readiness, properties(e) AS props, "
        "COLLECT { MATCH (e)-[d:DEMONSTRATED]->(s:Skill) RETURN s.id + ':' + toString(d.level) } "
        "AS shown",
        {"id": engagement_id},
    )


# ------------------------------------------------------------------ the seeded history
def test_the_demo_has_history_with_ratings_the_volunteer_can_read(web: Web):
    asha = {e["title"]: e for e in history(web, "asha")}
    assert set(asha) == {"Shelter Meal Rota", "Food Parcel Survey", "Winter Outreach Kitchen"}
    rota = asha["Shelter Meal Rota"]
    assert (rota["org_name"], rota["role_label"], rota["outcome_label"]) == (
        "Cyber Scallywags", "Team member", "Project completed",
    )  # fmt: skip
    assert (rota["readiness"], rota["readiness_label"]) == (3, "Can guide others")
    assert (
        rota["rated_by_name"] == "Gwen"
        and "showed two new volunteers" in rota["note_for_volunteer"]
    )
    assert rota["skills"] == [
        {
            "id": "cooking",
            "name": "Community cooking",
            "level": 3,
            "level_label": "Can guide others",
        }
    ]
    winter = asha["Winter Outreach Kitchen"]  # she leads it, and it is still running
    assert (winter["role"], winter["ended_at"], winter["outcome_label"]) == (
        "lead", None, "On the team now",
    )  # fmt: skip
    assert "private_note" not in rota
    [dev] = history(web, "dev")
    assert dev["title"] == "Food Parcel Survey" and dev["readiness"] is None  # not reviewed yet


def test_a_completed_project_is_history_in_the_graph_and_matches_nobody(web: Web):
    statuses = web.rt.graph.read(
        "MATCH (p:Project) RETURN p.status AS status, count(p) AS n ORDER BY status"
    )
    assert statuses == [{"status": "completed", "n": 2}, {"status": "published", "n": 4}]
    for statement, params in (
        (cypher.MATCH_CANDIDATES, cypher.match_params(orgs=[CS, DK])),
        (cypher.BROWSE_PROJECTS, {"org": CS, "project": ""}),
        (cypher.BROWSE_PROJECTS, {"org": DK, "project": ""}),
        (cypher.INSPECT, {}),
        (cypher.NEEDED_ELSEWHERE, {"volunteer": USERS["kofi"]}),
    ):
        text = str(web.rt.graph.read(statement, params))
        assert ROTA not in text and SURVEY not in text and "Shelter Meal Rota" not in text
    assert web.rt.graph.read(
        "MATCH (:Volunteer)-[r:ON_TEAM]->(p:Project) WHERE p.status = 'completed' "
        "RETURN count(r) AS n"
    ) == [{"n": 0}]
    web.sign_in("asha")
    assert "Shelter Meal Rota" not in web.get(f"/o/{CS}/projects").text
    assert as_(web, "asha").get("/placements").json()[0]["title"] == "Winter Outreach Kitchen"


# ------------------------------------------------------------------ engagements follow the team
def test_joining_and_leaving_a_team_opens_and_closes_an_engagement(web: Web):
    dev = USERS["dev"]
    assert [e["title"] for e in history(web, "dev")] == ["Food Parcel Survey"]
    invite(web, "cerys", FOOD_BANK, dev)
    assert len(history(web, "dev")) == 1  # an invitation is not an engagement
    accept(web, "dev", FOOD_BANK)
    first = engagement(web, FOOD_BANK, "dev")
    assert (first["role"], first["ended_at"], first["outcome"]) == ("contributor", None, None)
    [row] = graph_engagement(web, first["id"])
    assert (row["volunteer"], row["project"], row["outcome"]) == (dev, FOOD_BANK, None)

    as_(web, "dev").post(f"/placements/{FOOD_BANK}/leave")
    left = engagement(web, FOOD_BANK, "dev")
    assert left["id"] == first["id"] and left["outcome"] == "left" and left["ended_at"]
    assert graph_engagement(web, first["id"])[0]["outcome"] == "left"

    # Coming back is a second spell, with its own engagement.
    as_(web, "cerys").post(f"/projects/{FOOD_BANK}/placements/{dev}/reopen")
    invite(web, "cerys", FOOD_BANK, dev)
    accept(web, "dev", FOOD_BANK)
    spells = [e for e in history(web, "dev") if e["project_id"] == FOOD_BANK]
    assert len(spells) == 2 and len({e["id"] for e in spells}) == 2
    assert sorted(e["outcome"] or "open" for e in spells) == ["left", "open"]


def test_engagement_ids_depend_only_on_whose_spell_it_is(web: Web):
    assert engagement(web, ROTA, "asha")["id"] == engagements.engagement_id(ROTA, USERS["asha"], 0)
    assert engagements.engagement_id(ROTA, USERS["asha"], 1) != engagements.engagement_id(
        ROTA, USERS["asha"], 0
    )


def test_a_removed_volunteer_and_a_withdrawn_project_end_engagements(web: Web):
    bryn = USERS["bryn"]  # on the Winter Outreach team
    as_(web, "kofi").post(
        f"/projects/{WINTER}/placements/{bryn}/remove", {"reason": "Rota changed"}
    )
    assert engagement(web, WINTER, "bryn")["outcome"] == "removed"
    lead = engagement(web, WINTER, "asha")
    assert lead["outcome"] is None and graph_engagement(web, lead["id"])
    as_(web, "kofi").post(
        f"/orgs/{CS}/projects/{WINTER}/withdraw",
        {"reason": "Shelter closed", "version": version(web, WINTER)},
    )
    assert engagement(web, WINTER, "asha")["outcome"] == "withdrawn"  # kept in SQLite
    assert graph_engagement(web, lead["id"]) == []  # gone from the graph with the project


# ------------------------------------------------------------------ completing a project
def test_only_the_lead_side_completes_a_project(web: Web):
    body = {"version": version(web, FOOD_BANK)}
    path = f"/orgs/{DK}/projects/{FOOD_BANK}/complete"
    assert as_(web, "dev").post(path, body).status_code == 403  # a member, not the lead side
    assert as_(web, "kofi").post(path, body).status_code == 404  # another organisation
    assert as_(web, "cerys").post(path, {"version": 99}).status_code == 409  # stale
    assert (
        as_(web, "cerys").post(f"/orgs/{CS}/projects/{FOOD_BANK}/complete", body).status_code == 404
    )
    assert as_(web, "cerys").post(path, body).json() == {"state": "completed"}
    assert as_(web, "cerys").post(path, {"version": body["version"] + 1}).status_code == 409


def test_completing_a_project_turns_its_team_into_history(web: Web):
    dev = USERS["dev"]
    invite(web, "cerys", FOOD_BANK, dev)
    accept(web, "dev", FOOD_BANK)
    invite(web, "cerys", FOOD_BANK, USERS["asha"])  # invited, never answered
    r = as_(web, "cerys").post(
        f"/orgs/{DK}/projects/{FOOD_BANK}/complete", {"version": version(web, FOOD_BANK)}
    )
    assert r.status_code == 200, r.text
    assert placement(web, FOOD_BANK, dev)["state"] == "completed"
    assert placement(web, FOOD_BANK, USERS["asha"])["state"] == "lapsed"
    done = engagement(web, FOOD_BANK, "dev")
    assert (done["outcome"], done["outcome_label"]) == ("completed", "Project completed")
    assert engagement(web, FOOD_BANK, "cerys")["role"] == "lead"
    assert not any(e["project_id"] == FOOD_BANK for e in history(web, "asha"))
    [row] = graph_engagement(web, done["id"])
    assert (row["status"], row["outcome"], row["role"]) == ("completed", "completed", "contributor")
    assert graph_edges(web, dev, "ON_TEAM") == []
    assert candidates(web, DK, FOOD_BANK) == []  # no longer a project to match
    assert any("is complete. Thank you" in m for m in as_(web, "dev").messages())
    assert any("Your invitation to it has closed" in m for m in as_(web, "asha").messages())
    assert not any("is complete" in m for m in as_(web, "cerys").messages())  # she did it
    assert as_(web, "dev").get(f"/orgs/{DK}/projects").json() == []


# ------------------------------------------------------------------ rating
def test_the_lead_side_rates_an_ended_engagement_and_the_volunteer_sees_it(web: Web):
    target = engagement(web, SURVEY, "dev")
    cerys = as_(web, "cerys")  # led the survey
    r = cerys.post(
        f"/engagements/{target['id']}/rating",
        {"readiness": 1, "skills": {"data-analysis": 2}, "note": "Good start; pair up next time.",
         "private_note": "Needed a lot of checking."},
    )  # fmt: skip
    assert r.status_code == 200, r.text
    mine = as_(web, "dev").get("/me/history").json()
    [seen] = mine["engagements"]
    assert (seen["readiness"], seen["readiness_label"]) == (1, "Needs support")
    assert seen["rated_by_name"] == "Cerys" and seen["note_for_volunteer"].startswith("Good start")
    assert seen["skills"][0]["level_label"] == "Works independently"
    assert "Needed a lot of checking" not in str(mine)  # the private note is not theirs to read
    assert mine["levels"] == {
        "1": "Needs support", "2": "Works independently", "3": "Can guide others",
        "4": "Could lead a project",
    }  # fmt: skip
    assert any("has been reviewed: Needs support" in m for m in as_(web, "dev").messages())
    [row] = graph_engagement(web, target["id"])
    assert row["readiness"] == 1 and row["shown"] == ["data-analysis:2"]
    # Opaque and minimal in the graph: no note, no name, no dates.
    assert set(row["props"]) == {"id", "role", "outcome", "readiness"}
    assert "Needed a lot" not in str(web.rt.graph.read(cypher.ALL_NODES))
    # The lead side keeps its private note; rating again replaces the rating.
    listed = cerys.get(f"/orgs/{DK}/history").json()[0]["engagements"]
    assert next(e for e in listed if e["id"] == target["id"])["private_note"].startswith("Needed")
    cerys.post(f"/engagements/{target['id']}/rating", {"readiness": 2})
    again = graph_engagement(web, target["id"])[0]
    assert again["readiness"] == 2 and again["shown"] == []


def test_rating_is_held_to_its_rules(web: Web):
    dev_survey = engagement(web, SURVEY, "dev")["id"]
    own = engagement(web, SURVEY, "cerys")["id"]
    running = engagement(web, WINTER, "bryn")["id"]
    rate = lambda who, eid, **body: as_(web, who).post(  # noqa: E731
        f"/engagements/{eid}/rating", {"readiness": 3} | body
    )
    assert rate("cerys", own).status_code == 403  # nobody rates their own work
    assert rate("dev", dev_survey).status_code == 403  # a member, not the lead side
    assert rate("kofi", dev_survey).status_code == 404  # another organisation
    assert rate("asha", running).status_code == 409  # still on the team
    assert rate("cerys", dev_survey, readiness=5).status_code == 422
    assert rate("cerys", dev_survey, skills={"carpentry": 2}).status_code == 422  # not its skill
    assert rate("cerys", "e_nothing").status_code == 404
    assert rate("rhiannon", own).status_code == 200  # an org admin rates the lead
    assert engagement(web, SURVEY, "dev")["readiness"] is None  # nothing above went through


# ------------------------------------------------------------------ consent to share history
def test_another_organisation_sees_a_summary_only_with_the_volunteers_consent(web: Web):
    # Asha shares with DataKind UK. Its Food Bank lead sees her work there and a summary of her
    # work with Cyber Scallywags: the completed rota and the outreach kitchen she leads.
    asha = experience(web, DK, FOOD_BANK, "rhiannon")["Asha"]
    assert (asha["engagements"], asha["same_cause"], asha["elsewhere"], asha["led"]) == (3, 1, 2, 1)
    assert asha["best_readiness_label"] == "Can guide others"
    assert asha["summary"] == (
        "3 earlier projects on poverty and basic needs, 2 with another organisation, led 1, "
        "rated up to “Can guide others”"
    )
    assert graph_edges(web, USERS["asha"], "SHARES_HISTORY_WITH") == [DK]
    # She does not share with Cyber Scallywags: it has its own record of her and no more.
    row = next(
        r for r in web.rt.graph.read(cypher.CANDIDATE_EXPERIENCE, {"org": CS, "project": WINTER})
        if r["volunteer"] == USERS["asha"]
    )  # fmt: skip
    assert (row["engagements"], row["elsewhere"]) == (1, 0)
    # Switching it off removes the edge, and with it everything from elsewhere.
    assert as_(web, "asha").put(f"/orgs/{DK}/history-sharing", {"share": False}).json() == {
        "share_history": False
    }
    assert graph_edges(web, USERS["asha"], "SHARES_HISTORY_WITH") == []
    asha = experience(web, DK, FOOD_BANK, "rhiannon")["Asha"]
    assert (asha["engagements"], asha["elsewhere"], asha["led"]) == (1, 0, 0)
    assert asha["summary"] == (
        "1 earlier project on poverty and basic needs, rated up to “Can guide others”"
    )


def test_a_summary_never_names_the_other_organisation_or_its_projects(web: Web):
    rows = web.rt.graph.read(cypher.CANDIDATE_EXPERIENCE, {"org": DK, "project": ""})
    assert all(
        set(r) == {"project", "volunteer", "theme", "engagements", "same_cause", "elsewhere",
                   "led", "best_readiness"}
        for r in rows
    )  # fmt: skip
    body = as_(web, "rhiannon").get(f"/orgs/{DK}/coverage").text
    assert "Shelter Meal Rota" not in body and "Cyber Scallywags" not in body and CS not in body
    web.sign_in("rhiannon")
    page = web.get(f"/o/{DK}/coverage").text
    assert "done this before" in page and "2 with another organisation" in page
    assert "Shelter Meal Rota" not in page and "Winter Outreach" not in page


def test_only_the_volunteer_decides_and_only_for_an_organisation_they_are_with(web: Web):
    assert as_(web, "kofi").put(f"/orgs/{DK}/history-sharing", {"share": True}).status_code == 403
    with pytest.raises(services.Forbidden):
        engagements.set_share_history(web.rt.db, USERS["kofi"], DK, share=True)
    # Sharing is off until switched on, and leaving the organisation takes the edge away.
    assert graph_edges(web, USERS["dev"], "SHARES_HISTORY_WITH") == []
    as_(web, "dev").put(f"/orgs/{DK}/history-sharing", {"share": True})
    assert graph_edges(web, USERS["dev"], "SHARES_HISTORY_WITH") == [DK]
    as_(web, "dev").delete(f"/orgs/{DK}/membership")
    assert graph_edges(web, USERS["dev"], "SHARES_HISTORY_WITH") == []


# ------------------------------------------------------------------ skill levels
def test_a_volunteer_sets_their_own_level_and_it_reaches_the_graph(web: Web):
    def levels(key: str) -> dict:
        return {
            r["skill"]: r["level"]
            for r in web.rt.graph.read(
                "MATCH (:Volunteer {id: $id})-[h:HAS_SKILL]->(s:Skill) "
                "RETURN s.id AS skill, h.level AS level",
                {"id": USERS[key]},
            )
        }

    assert levels("dev") == {"bookkeeping": 3, "data-analysis": 2}  # from the fixture
    dev = as_(web, "dev")
    profile = dev.get("/profile").json()
    assert {s["id"]: s["level"] for s in profile["skills"] if s["level"]} == levels("dev")
    assert profile["levels"]["4"] == "Could lead a project"
    body = {
        "display_name": "Dev", "version": profile["version"], "causes": {},
        "skills": {"bookkeeping": "offers", "data-analysis": "none", "python": "has"},
    }  # fmt: skip
    # A client that does not send levels changes none; a level needs a skill they have.
    saved = dev.put("/profile", body).json()
    assert levels("dev") == {"bookkeeping": 3, "python": None}
    saved = dev.put(
        "/profile", body | {"version": saved["version"], "levels": {"python": 4, "cooking": 2}}
    ).json()
    assert levels("dev") == {"bookkeeping": None, "python": 4}
    assert (
        dev.put(
            "/profile", body | {"version": saved["version"], "levels": {"python": 7}}
        ).status_code
        == 422
    )
    # A level is the volunteer's own view: it changes no match.
    assert "Dev" in [c["name"] for c in candidates(web, DK, FOOD_BANK)]


def test_the_profile_form_saves_levels(web: Web):
    save_profile(web, "kofi")  # a form without level fields leaves the levels alone
    web.sign_in("kofi")
    page = web.get("/profile").text
    assert 'name="level:data-analysis"' in page and "Level: not said" in page
    current = services.get_profile(web.rt.db, USERS["kofi"])
    assert current.levels == {"data-analysis": 4, "fundraising": 3}
    form = {
        "display_name": "Kofi",
        "version": current.version,
        "skill:data-analysis": "offers",
        "level:data-analysis": "2",
        "skill:fundraising": "none",
        "level:fundraising": "3",
    }
    assert web.post("/profile", form).status_code == 303
    assert services.get_profile(web.rt.db, USERS["kofi"]).levels == {"data-analysis": 2}


# ------------------------------------------------------------------ the web pages
def test_the_profile_shows_history_and_the_sharing_switches(web: Web):
    web.sign_in("asha")
    page = web.get("/profile").text
    assert "Your history" in page and "Shelter Meal Rota" in page
    assert "Can guide others" in page and "from Gwen" in page
    assert 'name="share:' + DK + '" value="1" checked' in page
    assert 'name="share:' + CS + '" value="1" >' in page
    r = web.post("/me/history-sharing", {f"share:{CS}": "1"}, csrf_from="/profile")
    assert r.status_code == 303 and r.headers["location"].startswith("/profile")
    assert graph_edges(web, USERS["asha"], "SHARES_HISTORY_WITH") == [CS]  # DK unticked


def test_the_lead_completes_a_project_and_reviews_the_team_on_the_web(web: Web):
    invite(web, "cerys", FOOD_BANK, USERS["dev"])
    accept(web, "dev", FOOD_BANK)
    web.sign_in("cerys")
    page = web.get(f"/o/{DK}/projects/{FOOD_BANK}").text
    assert "Mark project as completed" in page
    r = web.post(
        f"/o/{DK}/projects/{FOOD_BANK}/complete", {"version": version(web, FOOD_BANK)},
        csrf_from=f"/o/{DK}/projects/{FOOD_BANK}",
    )  # fmt: skip
    assert r.status_code == 303 and r.headers["location"].startswith(f"/o/{DK}/history")
    page = web.get(f"/o/{DK}/history").text
    assert "Food Bank Demand Dashboard" in page and "Food Parcel Survey" in page
    assert "Someone else reviews your own work" in page  # her own rows have no form
    mine = engagement(web, FOOD_BANK, "dev")["id"]
    r = web.post(
        f"/o/{DK}/history/{mine}/rate",
        {"readiness": "2", "skill:bookkeeping": "3", "skill:data-analysis": "", "note": "Solid."},
        csrf_from=f"/o/{DK}/history",
    )
    assert r.status_code == 303
    rated = engagement(web, FOOD_BANK, "dev")
    assert rated["readiness"] == 2 and [s["id"] for s in rated["skills"]] == ["bookkeeping"]
    assert "Works independently" in web.get(f"/o/{DK}/history").text
    # A member who leads nothing sees no one's reviews.
    web.sign_in("dev")
    other = web.get(f"/o/{DK}/history").text
    assert "No completed projects for you to review" in other and "Solid." not in other
