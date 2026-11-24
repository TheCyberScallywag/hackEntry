"""The skills ontology: the catalogue file, its projection to the graph, and what reads it."""

import json

import pytest

from app import matching
from app.catalogue import get_catalogue, load_catalogue, text_query
from app.config import FIXTURES_DIR
from app.graph import cypher
from app.runtime import seed_catalogue
from tests.conftest import CS, DK, FOOD_BANK, IL, USERS, WINTER, Web
from tests.test_matching import save_profile
from tests.test_rbac_onboarding import as_


def _broken(tmp_path, change) -> None:
    raw = json.loads((FIXTURES_DIR / "catalogue.json").read_text(encoding="utf-8"))
    change(raw)
    path = tmp_path / "catalogue.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    load_catalogue(path)


def test_the_catalogue_is_a_two_level_ontology():
    cat = get_catalogue()
    areas = {a.id: a for a in cat.areas}
    tops = {a.id for a in cat.areas if a.parent is None}
    assert len(tops) == 4 and all(areas[a.parent].parent is None for a in cat.areas if a.parent)
    assert all(
        areas[s.area].parent in tops and s.category == areas[s.area].name for s in cat.skills
    )
    assert {c.theme for c in cat.causes} == {t.id for t in cat.themes}
    assert cat.related_to("python") == ("data-analysis", "web-development")  # read both ways
    assert cat.related_to("welsh-translation") == ()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda raw: raw["skills"][0].update(area="nowhere"), "unknown area"),
        (lambda raw: raw["skills"][0].update(area="digital-and-data"), "second-level"),
        (lambda raw: raw["skills"][0].update(kind="magic"), "unknown kind"),
        (lambda raw: raw["causes"][0].update(theme="nowhere"), "unknown theme"),
        (lambda raw: raw["related"].append(["python", "juggling"]), "unknown skill"),
        (lambda raw: raw["related"].append(["python", "cooking"]), "alphabetical"),
        (lambda raw: raw["related"].append(list(raw["related"][0])), "listed twice"),
        (lambda raw: raw["areas"].append({"id": "deep", "name": "Deep", "parent": "data"}), "two"),
    ],
)
def test_a_catalogue_with_a_broken_reference_is_refused(tmp_path, change, message):
    with pytest.raises(ValueError, match=message):
        _broken(tmp_path, change)


def test_the_ontology_is_in_the_graph(rt):
    cat = get_catalogue()
    in_area = rt.graph.read(
        "MATCH (s:Skill)-[:IN_AREA]->(a:SkillArea)-[:BROADER]->(top:SkillArea) "
        "WHERE NOT (top)-[:BROADER]->() "
        "RETURN s.id AS skill, a.id AS area, top.id AS top, s.kind AS kind, s.alt_labels AS alt"
    )
    areas = {a.id: a for a in cat.areas}
    assert {(r["skill"], r["area"], r["top"]) for r in in_area} == {
        (s.id, s.area, areas[s.area].parent) for s in cat.skills
    }
    assert {r["skill"]: (r["kind"], tuple(r["alt"])) for r in in_area} == {
        s.id: (s.kind, s.alt_labels) for s in cat.skills
    }
    related = rt.graph.read("MATCH (a:Skill)-[:RELATED_TO]->(b:Skill) RETURN a.id AS a, b.id AS b")
    assert {(r["a"], r["b"]) for r in related} == set(cat.related)
    themes = rt.graph.read(
        "MATCH (c:Cause)-[:IN_THEME]->(t:CauseTheme) RETURN c.id AS c, t.id AS t"
    )
    assert {r["c"]: r["t"] for r in themes} == {c.id: c.theme for c in cat.causes}


def test_reseeding_an_unchanged_catalogue_enqueues_nothing(rt):
    seed_catalogue(rt)
    assert rt.projector.status().pending == 0


def test_a_removed_area_and_a_removed_pair_leave_the_graph(rt):
    with rt.db.tx() as conn:
        conn.execute("INSERT INTO skill_areas VALUES ('spare', 'Spare', 'digital-and-data')")
        conn.execute("INSERT INTO outbox (kind, entity_id, created_at, next_attempt_at) "
                     "VALUES ('catalogue', 'catalogue', '', '')")  # fmt: skip
    rt.projector.drain(ignore_backoff=True)
    count = "MATCH (a:SkillArea {id: 'spare'}) RETURN count(a) AS n"
    assert rt.graph.read(count) == [{"n": 1}]
    with rt.db.tx() as conn:
        conn.execute("DELETE FROM skill_areas WHERE id = 'spare'")
        conn.execute("DELETE FROM skill_related WHERE skill_id = 'carpentry'")
        conn.execute("INSERT INTO outbox (kind, entity_id, created_at, next_attempt_at) "
                     "VALUES ('catalogue', 'catalogue', '', '')")  # fmt: skip
    assert rt.projector.drain(ignore_backoff=True).failed == 0
    assert rt.graph.read(count) == [{"n": 0}]
    assert rt.graph.read(
        "MATCH (:Skill {id: 'carpentry'})-[r:RELATED_TO]-() RETURN count(r) AS n"
    ) == [{"n": 0}]


def test_skills_are_found_by_their_other_names(rt):
    def find(words: str) -> list[str]:
        return [r["skill"] for r in rt.graph.read(cypher.FIND_SKILLS, {"query": text_query(words)})]

    assert find("dashboards") == ["data-visualisation"]
    assert set(find("Excel, and phishing!")) == {"data-analysis", "security-awareness"}
    assert find("juggling") == []
    [row] = rt.graph.read(cypher.FIND_SKILLS, {"query": text_query("python")})
    assert (row["area"], row["broader_area"]) == ("Software and web", "Digital and data")
    assert row["related"] == ["data-analysis", "web-development"]


def test_text_query_keeps_only_plain_words():
    assert text_query("Excel AND (phishing) OR ~name:*") == "excel OR phishing OR name"
    assert text_query(" -- ") == ""
    assert len(text_query(" ".join(f"w{i}" for i in range(40))).split(" OR ")) == 12


def _close(web: Web, org: str, project: str, reviewer: str = "kofi") -> dict[str, list]:
    cov = matching.project_coverage(web.rt.db, web.rt.graph, USERS[reviewer], org, project)
    return {
        row["skill"]: [(c["name"], c["via_names"]) for c in row["close"]]
        for row in cov.projects[0]["per_skill"]
    }


def test_close_lists_volunteers_who_offer_an_adjacent_skill(web: Web):
    # Related skills: Farah offers fundraising and Asha data analysis, both next to bookkeeping;
    # Cerys offers bookkeeping, next to data analysis.
    assert _close(web, DK, FOOD_BANK, "rhiannon") == {
        "Bookkeeping": [("Farah", ["Fundraising"]), ("Asha", ["Spreadsheet and data analysis"])],
        "Spreadsheet and data analysis": [("Cerys", ["Bookkeeping"])],
    }
    # Same area: carpentry and gardening are both practical trades.
    garden = web.rt.graph.read(cypher.CLOSE_CANDIDATES, {"org": IL, "project": ""})
    assert [(r["skill"], r["volunteer"], r["via"]) for r in garden] == [
        ("gardening", USERS["bryn"], ["carpentry"])
    ]


def test_close_never_makes_anyone_a_candidate_and_respects_exclusions(web: Web):
    close = _close(web, CS, WINTER)
    assert close["First aid"] == [("Asha", ["Community cooking"])]  # cooking is related
    # Gwen offers cooking too, but excluded a skill the project requires: the hard rule holds.
    assert all("Gwen" not in [n for n, _ in people] for people in close.values())
    cov = matching.project_coverage(web.rt.db, web.rt.graph, USERS["kofi"], CS, WINTER)
    first_aid = next(r for r in cov.projects[0]["per_skill"] if r["skill"] == "First aid")
    assert "Asha" not in [c["name"] for c in first_aid["candidates"]]
    assert "First aid" in cov.projects[0]["gaps"]  # close does not close a gap
    # A skill the volunteer has but does not offer is not a way in, and neither is an
    # excluded cause.
    save_profile(web, "asha", skills={"cooking": "has"})
    assert _close(web, CS, WINTER)["First aid"] == []
    save_profile(web, "asha", skills={"cooking": "offers"}, causes={"homelessness": "exclude"})
    assert _close(web, CS, WINTER)["First aid"] == []


def test_close_is_on_the_coverage_page_and_in_the_api(web: Web):
    web.sign_in("kofi")
    page = web.get(f"/o/{CS}/coverage").text
    assert "<th>Close</th>" in page and "via Community cooking" in page
    body = as_(web, "kofi").get(f"/orgs/{CS}/coverage").json()
    rows = {r["skill"]: r["close"] for r in body["projects"][0]["per_skill"]}
    assert rows["First aid"] == [
        {
            "volunteer": USERS["asha"],
            "name": "Asha",
            "via": ["Community cooking"],
            "wants_to_learn": True,  # the fixture says she wants to learn first aid
        }
    ]
