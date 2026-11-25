"""Learning goals: a volunteer says which skills they want to get better at. It is a wish, never
a reason to be matched."""

from app import matching, services
from app.graph import cypher
from tests.conftest import CS, DK, FOOD_BANK, USERS, WINTER, Web
from tests.test_matching import candidates, mine, save_profile
from tests.test_rbac_onboarding import as_, full_profile, graph_edges


def learn(web: Web, key: str) -> list[tuple]:
    found = matching.learning_opportunities(web.rt.db, web.rt.graph, USERS[key])
    return [(o["title"], o["skill_name"], o["mentors"], o["on_team"]) for o in found]


def test_wanting_to_learn_is_in_the_profile_and_the_graph(web: Web):
    assert services.get_profile(web.rt.db, USERS["kofi"]).learning == ["cooking"]  # the fixture
    assert graph_edges(web, USERS["kofi"], "WANTS_TO_LEARN") == ["cooking"]
    kofi = as_(web, "kofi")
    body = full_profile(kofi)
    assert [s["id"] for s in kofi.get("/profile").json()["skills"] if s["wants_to_learn"]] == [
        "cooking"
    ]
    # A client that does not send `learning` changes nothing.
    saved = kofi.put("/profile", body).json()
    assert graph_edges(web, USERS["kofi"], "WANTS_TO_LEARN") == ["cooking"]
    # A skill they exclude cannot also be one they want to learn.
    body = body | {"version": saved["version"], "learning": ["python", "first-aid", "gardening"]}
    body["skills"] = body["skills"] | {"first-aid": "exclude"}
    saved = kofi.put("/profile", body).json()
    assert graph_edges(web, USERS["kofi"], "WANTS_TO_LEARN") == ["gardening", "python"]
    bad = body | {"version": saved["version"], "learning": ["juggling"]}
    assert kofi.put("/profile", bad).status_code == 422


def test_wanting_to_learn_a_skill_never_makes_a_match(web: Web):
    # Dev wants to learn data visualisation and Kofi cooking; neither has the skill.
    assert "Kofi" not in [c["name"] for c in candidates(web, CS, WINTER)]
    assert not any(m["project"] == WINTER for m in mine(web, "kofi", [CS]).matches)
    rows = web.rt.graph.read(cypher.MATCH_CANDIDATES, cypher.match_params(orgs=[CS, DK]))
    assert all("cooking" not in r["matched"] or r["volunteer"] != USERS["kofi"] for r in rows)


def test_it_shows_projects_that_use_the_skill_and_who_could_guide_it_as_a_number(web: Web):
    # Winter Outreach needs cooking. Asha, its lead, has it at level 3: one person who can guide.
    assert learn(web, "kofi") == [("Winter Outreach Kitchen", "Community cooking", 1, False)]
    # Asha wants first aid, which her own project needs and nobody on the team can guide.
    assert learn(web, "asha") == [("Winter Outreach Kitchen", "First aid", 0, True)]
    assert learn(web, "dev") == []  # no published project of DataKind UK uses the skill
    rows = web.rt.graph.read(
        cypher.LEARNING_OPPORTUNITIES, {"volunteer": USERS["kofi"], "orgs": [CS]}
    )
    assert "u_" not in str(rows)  # a number of mentors, never who
    # Someone who says they need support with a skill is not counted as able to guide it.
    save_profile(web, "asha")
    asha = as_(web, "asha")
    asha.put("/profile", full_profile(asha) | {"levels": {"cooking": 2}})
    assert learn(web, "kofi") == [("Winter Outreach Kitchen", "Community cooking", 0, False)]


def test_it_respects_exclusions_membership_and_vetting(web: Web):
    save_profile(web, "kofi", causes={"homelessness": "exclude"})
    assert learn(web, "kofi") == []
    save_profile(web, "kofi", causes={"homelessness": "neutral"}, skills={"van-driving": "exclude"})
    assert learn(web, "kofi") == []  # the project requires a skill he excluded
    save_profile(web, "kofi", skills={"van-driving": "none"})
    assert len(learn(web, "kofi")) == 1
    as_(web, "kofi").delete(f"/orgs/{CS}/membership")
    assert learn(web, "kofi") == []  # only in organisations he volunteers with
    assert (
        matching.learning_opportunities(web.rt.db, web.rt.graph, USERS["kofi"], [CS]) == []
    )  # even if a caller names the organisation, the graph decides


def test_close_candidates_who_want_to_learn_the_skill_come_first(web: Web):
    # Farah (fundraising) and Asha (data analysis) are both close to bookkeeping.
    def close() -> list[tuple]:
        cov = matching.project_coverage(web.rt.db, web.rt.graph, USERS["rhiannon"], DK, FOOD_BANK)
        row = next(r for r in cov.projects[0]["per_skill"] if r["skill"] == "Bookkeeping")
        return [(c["name"], c["wants_to_learn"]) for c in row["close"]]

    assert close() == [("Farah", False), ("Asha", False)]
    asha = as_(web, "asha")
    asha.put("/profile", full_profile(asha) | {"learning": ["bookkeeping"]})
    assert close() == [("Asha", True), ("Farah", False)]
    web.sign_in("rhiannon")
    assert "wants to learn it" in web.get(f"/o/{DK}/coverage").text


def test_it_is_on_the_profile_form_the_matches_page_and_in_the_api(web: Web):
    web.sign_in("kofi")
    page = web.get("/profile").text
    assert (
        'name="learn:cooking" value="1" aria-label="I want to learn Community cooking" checked'
        in page
    )
    matches = web.get("/matches?scope=all").text
    assert "Where you could learn" in matches and "1 person on the team can guide it" in matches
    assert "Asha" not in matches
    assert as_(web, "kofi").get("/me/learning").json() == [
        {"org_id": CS, "organisation": "Cyber Scallywags", "project": WINTER,
         "title": "Winter Outreach Kitchen", "skill": "Community cooking", "mentors": 1,
         "on_team": False}
    ]  # fmt: skip
    # Unticking every box on the form clears them; a form without the boxes leaves them alone.
    current = services.get_profile(web.rt.db, USERS["kofi"])
    form = {"display_name": "Kofi", "version": current.version, "learning_form": "1",
            "skill:data-analysis": "offers", "learn:python": "1"}  # fmt: skip
    assert web.post("/profile", form).status_code == 303
    assert services.get_profile(web.rt.db, USERS["kofi"]).learning == ["python"]
    save_profile(web, "kofi")
    assert services.get_profile(web.rt.db, USERS["kofi"]).learning == ["python"]
    web.sign_in("kofi")
    assert "Where you could learn" not in web.get("/matches?scope=all").text
