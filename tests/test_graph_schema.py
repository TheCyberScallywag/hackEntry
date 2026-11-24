"""The graph's shape is defined once (app.graph.cypher) and everything else is derived from it.
These tests need no AuraDB: the push and the parity check run against a recording stand-in for
the Neo4j driver."""

import shutil
from pathlib import Path

import pytest

from app.config import BUNDLED_GRAPH, Settings
from app.graph import aura, cypher
from app.graph.store import GraphStore
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
    assert counts == {"nodes": 49, "relationships": 97}
    statements = [q for q, _ in driver.queries]
    assert statements[0].startswith("MATCH (n {dvn_scope: $scope}) DETACH DELETE n")
    assert any("MERGE (a)-[:ON_TEAM]->(b)" in q for q in statements)
    merged = sum(len(p["rows"]) for q, p in driver.queries if "MERGE (a)-[" in q)
    assert merged == 97
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
