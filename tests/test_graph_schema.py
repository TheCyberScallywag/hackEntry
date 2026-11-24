"""The graph's shape is defined once (app.graph.cypher) and everything else is derived from it.
These tests need no AuraDB: the push and the parity check run against a recording stand-in for
the Neo4j driver."""

import shutil
from pathlib import Path

import pytest

from app.config import BUNDLED_GRAPH, Settings
from app.graph import aura, cypher
from app.graph.projector import Projector
from app.graph.store import GraphError, GraphStore
from app.sqlstore import enqueue
from tests.conftest import CS, DK

SHAPES = "MATCH (a)-[r]->(b) RETURN DISTINCT labels(a)[0] AS a, type(r) AS t, labels(b)[0] AS b"


@pytest.fixture
def demo_graph(tmp_path: Path) -> GraphStore:
    path = tmp_path / "demo.rl"
    shutil.copyfile(BUNDLED_GRAPH, path)  # never open the committed file for writing
    path.chmod(0o600)
    return GraphStore(path)


class _Record:
    def __init__(self, row: dict) -> None:
        self._row = row

    def data(self) -> dict:
        return self._row


class FakeDriver:
    """Records every query; answers MATCH_CANDIDATES from a local store, like a perfect copy."""

    def __init__(self, mirror: GraphStore | None = None) -> None:
        self.queries: list[tuple[str, dict]] = []
        self.mirror = mirror

    def __enter__(self) -> FakeDriver:
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def execute_query(self, query: str, params: dict | None = None, **kwargs):
        self.queries.append((query, {**(params or {}), **kwargs}))
        rows = self.mirror.read(query, params) if self.mirror and params else []
        return [_Record(r) for r in rows], None, None


def test_schema_covers_exactly_what_the_demo_graph_contains(demo_graph: GraphStore):
    found = {r["t"]: (r["a"], r["b"]) for r in demo_graph.read(SHAPES)}
    assert found == cypher.RELATIONSHIPS  # ON_TEAM included; no entry the graph never uses
    labels = {r["label"] for r in demo_graph.read(cypher.COUNT_LABELS)}
    assert labels == set(cypher.LABELS)


def test_every_label_has_a_uniqueness_constraint_in_the_bundled_graph(demo_graph: GraphStore):
    found = {r["name"]: r for r in demo_graph.read(cypher.SHOW_CONSTRAINTS)}
    assert set(found) == set(cypher.CONSTRAINTS) | set(cypher.TYPE_CONSTRAINTS)
    unique = [found[name] for name in cypher.CONSTRAINTS]
    assert {r["labels"][0] for r in unique} == set(cypher.LABELS)
    assert all(
        r["type"] == "NODE_PROPERTY_UNIQUENESS" and r["properties"] == ["id"] for r in unique
    )
    # The one scale is compared by number, so the graph refuses anything else.
    assert {found[n]["type"] for n in cypher.TYPE_CONSTRAINTS} == {
        "NODE_PROPERTY_TYPE",
        "RELATIONSHIP_PROPERTY_TYPE",
    }
    indexes = {r["name"]: r for r in demo_graph.read(cypher.SHOW_INDEXES)}
    assert set(cypher.INDEXES) <= set(indexes)  # each constraint also owns an index
    assert all(r["state"] == "ONLINE" for r in indexes.values())


def test_a_duplicate_id_is_refused(demo_graph: GraphStore):
    with pytest.raises(GraphError, match="uniqueness"), demo_graph.write() as tx:
        tx.run("CREATE (:Skill {id: 'python'})")
    assert demo_graph.read("MATCH (s:Skill {id: 'python'}) RETURN count(s) AS n") == [{"n": 1}]


def test_a_store_made_before_the_schema_gains_it_on_the_next_drain(rt, tmp_path):
    old = GraphStore(tmp_path / "old.rl")
    with old.write() as tx:
        tx.run("CREATE (:Skill {id: 'python', name: 'Python programming'})")
    assert old.read(cypher.SHOW_CONSTRAINTS) == []
    projector = Projector(rt.db, old)
    assert projector.drain().failed == 0  # nothing is pending: the schema is still declared
    declared = set(cypher.CONSTRAINTS) | set(cypher.TYPE_CONSTRAINTS)
    assert {r["name"] for r in old.read(cypher.SHOW_CONSTRAINTS)} == declared
    projector.ensure_schema()  # idempotent
    assert len(old.read(cypher.SHOW_CONSTRAINTS)) == len(declared)


def test_a_store_whose_schema_cannot_be_declared_is_not_written_to(rt, tmp_path):
    bad = GraphStore(tmp_path / "bad.rl")
    with bad.write() as tx:  # two skills with one id: the constraint cannot be created
        tx.run("CREATE (:Skill {id: 'python'}), (:Skill {id: 'python'})")
    with rt.db.tx() as conn:
        enqueue(conn, "catalogue", "catalogue")
    projector = Projector(rt.db, bad)
    assert projector.drain(ignore_backoff=True).failed == 1
    status = projector.status()
    assert status.pending == 1 and status.items[0]["last_error"]
    assert bad.read("MATCH (n) RETURN count(n) AS n") == [{"n": 2}]


def test_a_level_that_is_not_a_number_is_refused(demo_graph: GraphStore):
    with pytest.raises(GraphError), demo_graph.write() as tx:
        tx.run("MATCH (:Volunteer)-[h:HAS_SKILL]->(:Skill) WITH h LIMIT 1 SET h.level = 'high'")
    with pytest.raises(GraphError), demo_graph.write() as tx:
        tx.run("MATCH (e:Engagement) WITH e LIMIT 1 SET e.readiness = 'good'")


def test_push_carries_relationship_properties(demo_graph, monkeypatch):
    driver = FakeDriver()
    monkeypatch.setattr(aura, "_driver", lambda _settings: driver)
    aura.push(Settings(_env_file=None), demo_graph)
    [(query, params)] = [(q, p) for q, p in driver.queries if "[r:HAS_SKILL]" in q]
    assert "SET r += row.props" in query
    assert {row["props"].get("level") for row in params["rows"]} >= {2, 3, 4}


def test_reconcile_statements_are_generated_for_every_schema_entry():
    assert set(cypher.VOLUNTEER_EDGE_PRUNE) == set(cypher.VOLUNTEER_EDGE_MERGE)
    assert set(cypher.VOLUNTEER_EDGE_MERGE) == set(cypher.VOLUNTEER_RELS)
    assert set(cypher.PROJECT_EDGE_PRUNE) == set(cypher.PROJECT_EDGE_MERGE)
    assert set(cypher.PROJECT_EDGE_MERGE) == set(cypher.PROJECT_RELS)
    assert "-[r:HAS_SKILL]->(t:Skill)" in cypher.VOLUNTEER_EDGE_PRUNE["HAS_SKILL"]
    assert "MERGE (n)-[:SERVES_CAUSE]->(t)" in cypher.PROJECT_EDGE_MERGE["SERVES_CAUSE"]


def test_push_writes_the_whole_demo_graph_including_teams(demo_graph, monkeypatch):
    driver = FakeDriver()
    monkeypatch.setattr(aura, "_driver", lambda _settings: driver)
    counts = aura.push(Settings(_env_file=None), demo_graph)
    assert counts == {"nodes": 80, "relationships": 173}
    statements = [q for q, _ in driver.queries]
    assert statements[0].startswith("MATCH (n {dvn_scope: $scope}) DETACH DELETE n")
    assert any("MERGE (a)-[r:ON_TEAM]->(b)" in q for q in statements)
    merged = sum(len(p["rows"]) for q, p in driver.queries if "MERGE (a)-[" in q)
    assert merged == 173
    assert all(p["scope"] == aura.SCOPE for _, p in driver.queries)


def test_push_refuses_an_unknown_relationship_before_touching_the_destination(
    demo_graph, monkeypatch
):
    with demo_graph.write() as tx:
        tx.run("MATCH (v:Volunteer), (c:Cause) WITH v, c LIMIT 1 CREATE (v)-[:ADMIRES]->(c)")
    driver = FakeDriver()
    monkeypatch.setattr(aura, "_driver", lambda _settings: driver)
    with pytest.raises(ValueError, match="Volunteer-ADMIRES->Cause"):
        aura.push(Settings(_env_file=None), demo_graph)
    assert driver.queries == []  # nothing was deleted or written


def test_push_refuses_a_known_type_between_the_wrong_labels(demo_graph, monkeypatch):
    with demo_graph.write() as tx:
        tx.run("MATCH (v:Volunteer), (c:Cause) WITH v, c LIMIT 1 CREATE (v)-[:HAS_SKILL]->(c)")
    monkeypatch.setattr(aura, "_driver", lambda _settings: FakeDriver())
    with pytest.raises(ValueError, match="Volunteer-HAS_SKILL->Cause"):
        aura.push(Settings(_env_file=None), demo_graph)


def test_parity_runs_the_same_bounded_query_on_both_sides(demo_graph, monkeypatch):
    driver = FakeDriver(mirror=demo_graph)
    monkeypatch.setattr(aura, "_driver", lambda _settings: driver)
    report = aura.parity(Settings(_env_file=None), demo_graph, [DK, CS])
    assert report["match"] is True
    assert report["organisations"][DK] == {"local": 3, "aura": 3, "same": True}
    assert report["organisations"][CS] == {"local": 2, "aura": 2, "same": True}
    query, params = driver.queries[0]
    assert query == cypher.MATCH_CANDIDATES
    assert params["limit"] == demo_graph.max_rows + 1 and params["orgs"] == [DK]


def test_parity_reports_a_difference(demo_graph, monkeypatch, tmp_path):
    other = tmp_path / "other.rl"
    shutil.copyfile(demo_graph.path, other)
    drifted = GraphStore(other)
    with drifted.write() as tx:
        tx.run("MATCH (:Volunteer)-[r:OFFERS_SKILL]->(:Skill) DELETE r")
    monkeypatch.setattr(aura, "_driver", lambda _settings: FakeDriver(mirror=drifted))
    report = aura.parity(Settings(_env_file=None), demo_graph, [DK])
    assert report["match"] is False and report["organisations"][DK]["aura"] == 0


def test_aura_needs_explicit_credentials():
    with pytest.raises(aura.AuraNotConfigured):
        aura._driver(Settings(aura_uri="", _env_file=None))
    with pytest.raises(aura.AuraNotConfigured):
        aura._driver(Settings(aura_uri="bolt://localhost", aura_password="x", _env_file=None))
