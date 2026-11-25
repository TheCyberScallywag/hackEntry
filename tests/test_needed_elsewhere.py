"""Where am I needed? The one question that crosses organisations: organisations a volunteer has
not joined that need a skill they offer. It must respect every exclusion and never return a
person."""

from app import matching
from app.graph import cypher
from tests.conftest import CS, DK, IL, USERS, WINTER, Web
from tests.test_matching import save_profile
from tests.test_rbac_onboarding import NIA, as_, placement


def needed(web: Web, key: str) -> list[tuple]:
    result = matching.needed_elsewhere(web.rt.db, web.rt.graph, USERS[key])
    assert result.withheld is None
    return [(o["org_name"], o["skill_name"], o["cause_name"]) for o in result.opportunities]


def test_it_finds_organisations_not_joined_that_need_a_skill_the_volunteer_offers(web: Web):
    # Kofi volunteers with Cyber Scallywags only; DataKind UK's dashboard needs data analysis
    # and its team (Cerys, the lead) does not cover it.
    assert needed(web, "kofi") == [
        ("DataKind UK", "Spreadsheet and data analysis", "Food Security")
    ]
    assert needed(web, "farah") == [("Cyber Scallywags", "First aid", "Homelessness")]
    # Asha is already in both organisations whose projects she could help; Dev offers nothing
    # another organisation requires.
    assert needed(web, "asha") == [] and needed(web, "dev") == []


def test_it_never_returns_an_organisation_the_volunteer_is_in(web: Web):
    web.sign_in("kofi")
    web.post(f"/orgs/{DK}/join")
    assert needed(web, "kofi") == []


def test_exclusions_apply_exactly_as_in_matching(web: Web):
    save_profile(web, "kofi", causes={"food-security": "exclude"})
    assert needed(web, "kofi") == []  # an excluded cause
    save_profile(
        web, "kofi", causes={"food-security": "neutral"}, skills={"bookkeeping": "exclude"}
    )
    assert needed(web, "kofi") == []  # the project also requires a skill he excluded
    save_profile(web, "kofi", skills={"bookkeeping": "none", "data-analysis": "has"})
    assert needed(web, "kofi") == []  # a skill he has but does not offer is not a way in
    save_profile(
        web, "kofi", skills={"data-analysis": "offers"}, causes={"food-security": "prefer"}
    )
    result = matching.needed_elsewhere(web.rt.db, web.rt.graph, USERS["kofi"])
    assert [o["preferred"] for o in result.opportunities] == [True]


def test_a_skill_the_team_already_covers_is_not_a_need(web: Web):
    assert needed(web, "farah") == [("Cyber Scallywags", "First aid", "Homelessness")]
    # Bryn is on the Winter Outreach team. Once he offers first aid, the team covers it.
    save_profile(web, "bryn", skills={"first-aid": "offers"})
    assert needed(web, "farah") == []


def test_only_vetted_volunteers_are_answered_and_results_wait_for_the_projection(web: Web):
    nia = matching.needed_elsewhere(web.rt.db, web.rt.graph, NIA)
    assert nia.opportunities == [] and "in screening" in nia.withheld.reason.lower()
    with web.rt.db.tx() as conn:
        conn.execute("INSERT INTO outbox (kind, entity_id, created_at, next_attempt_at) "
                     "VALUES ('volunteer', ?, '', '9999')", (USERS["kofi"],))  # fmt: skip
    pending = matching.needed_elsewhere(web.rt.db, web.rt.graph, USERS["kofi"])
    assert pending.opportunities == [] and "still being applied" in pending.withheld.reason


def test_a_project_withdrawn_in_sqlite_is_dropped_even_if_the_graph_still_has_it(web: Web):
    from tests.conftest import FOOD_BANK

    with web.rt.db.tx() as conn:  # SQLite is ahead of the graph: no projection has run
        conn.execute("UPDATE submissions SET state = 'withdrawn' WHERE id = ?", (FOOD_BANK,))
    assert needed(web, "kofi") == []


def test_the_query_returns_no_person_and_no_project_title(web: Web):
    rows = web.rt.graph.read(cypher.NEEDED_ELSEWHERE, {"volunteer": USERS["kofi"]})
    assert rows and all(set(r) == {"org", "skill", "cause", "preferred", "projects"} for r in rows)
    text = str(rows)
    assert "u_" not in text and "Dashboard" not in text


def test_it_is_on_the_organisations_page_and_in_the_api(web: Web):
    web.sign_in("kofi")
    page = web.get("/orgs").text
    assert "Where you are needed" in page
    assert (
        "<strong>DataKind UK</strong> needs <strong>Spreadsheet and data analysis</strong>" in page
    )
    assert "Cerys" not in page and "Food Bank Demand Dashboard" not in page
    web.sign_in("asha")
    assert "Where you are needed" not in web.get("/orgs").text
    body = as_(web, "farah").get("/me/needed").json()
    assert body == {
        "withheld": None,
        "opportunities": [
            {"org_id": CS, "organisation": "Cyber Scallywags", "skill": "First aid",
             "cause": "Homelessness", "preferred_cause": False, "projects": 1}
        ],
    }  # fmt: skip
    assert IL not in str(body) and placement(web, WINTER, USERS["farah"]) is None
