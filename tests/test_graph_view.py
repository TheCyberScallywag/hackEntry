"""GET /admin/graph/view: the graph as nodes and relationships, for the admin console to draw."""

from tests.conftest import DK, USERS, Web
from tests.test_rbac_onboarding import as_


def test_the_view_is_the_whole_graph_with_names_for_the_admin(web: Web):
    view = as_(web, "sam").get("/admin/graph/view").json()
    assert view["too_large"] is False and view["max_rows"] == 500
    assert len(view["nodes"]) == sum(row["nodes"] for row in view["labels"]) == 80
    assert len(view["edges"]) == sum(e["relationships"] for e in view["model"]) == 176
    keys = {n["key"] for n in view["nodes"]}
    assert len(keys) == 80  # a key is unique where an id alone need not be
    assert all(e["source"] in keys and e["target"] in keys for e in view["edges"])
    captions = {n["key"]: n["caption"] for n in view["nodes"]}
    assert captions[f"Volunteer:{USERS['asha']}"] == "Asha"  # the graph itself holds only the ID
    assert captions[f"Organisation:{DK}"] == "DataKind UK"
    assert "lead · completed" in {n["caption"] for n in view["nodes"] if n["label"] == "Engagement"}
    level = next(e for e in view["edges"] if e["type"] == "HAS_SKILL"
                 and e["source"] == f"Volunteer:{USERS['asha']}")  # fmt: skip
    assert level["props"]["level"] in (1, 2, 3, 4)
    assert "@" not in str(view)  # no addresses reach the drawing


def test_the_view_can_be_narrowed_to_some_labels(web: Web):
    sam = as_(web, "sam")
    view = sam.get("/admin/graph/view?label=Skill&label=SkillArea").json()
    assert view["shown"] == ["Skill", "SkillArea"]
    assert {n["label"] for n in view["nodes"]} == {"Skill", "SkillArea"}
    assert {e["type"] for e in view["edges"]} == {"IN_AREA", "BROADER", "RELATED_TO"}
    assert len(view["model"]) == 25  # the model is always the whole graph's
    r = sam.get("/admin/graph/view?label=Nonsense")
    assert r.status_code == 422 and "Unknown label: Nonsense" in r.text


def test_a_view_past_the_row_bound_is_reported_not_cut_short(web: Web):
    web.rt.graph.max_rows = 100
    sam = as_(web, "sam")
    view = sam.get("/admin/graph/view").json()
    assert view["too_large"] is True and view["nodes"] == [] and view["edges"] == []
    assert view["max_rows"] == 100 and len(view["model"]) == 25
    narrowed = sam.get("/admin/graph/view?label=Organisation&label=Project").json()
    assert narrowed["too_large"] is False and len(narrowed["nodes"]) == 9


def test_only_a_system_admin_sees_the_view(web: Web):
    for key in ("mo", "kofi", "asha"):
        assert as_(web, key).get("/admin/graph/view").status_code == 403
    assert web.client.get("/api/v1/admin/graph/view").status_code == 401
