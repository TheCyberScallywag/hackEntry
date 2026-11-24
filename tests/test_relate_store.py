"""The Relate adapter against the real embedded engine (relate-db 1.0.1)."""

import shutil
import subprocess

import pytest
import relate

from app.cli import build_bundle
from app.config import BUNDLED_GRAPH
from app.graph import cypher
from app.graph.store import ENGINE_VERSION, GraphBusy, GraphError, GraphStore, GraphTruncated


def test_engine_version_is_pinned():
    from importlib.metadata import version

    assert version("relate-db") == ENGINE_VERSION == "1.0.1"


def test_write_close_reopen(tmp_path):
    store = GraphStore(tmp_path / "check.rl")
    with store.write() as tx:
        tx.run("MERGE (n:Check {id: $id}) SET n.v = $v", {"id": "x", "v": [1, 2]})
    assert (tmp_path / "check.rl").is_file()  # a single-file store
    reopened = GraphStore(tmp_path / "check.rl")
    assert reopened.read("MATCH (n:Check) RETURN n.id AS id, n.v AS v") == [
        {"id": "x", "v": [1, 2]}
    ]


def test_failed_write_rolls_back(tmp_path):
    store = GraphStore(tmp_path / "rb.rl")
    with store.write() as tx:
        tx.run("CREATE (:Check {id: 'kept'})")
    with pytest.raises(GraphError), store.write() as tx:
        tx.run("CREATE (:Check {id: 'discarded'})")
        tx.run("THIS IS NOT CYPHER")
    assert store.read("MATCH (n:Check) RETURN n.id AS id") == [{"id": "kept"}]


def test_locked_store_is_retried_then_reported(tmp_path):
    path = tmp_path / "busy.rl"
    with GraphStore(path).write() as tx:
        tx.run("CREATE (:Check)")
    holder = relate.connect(str(path))  # another opener holds the exclusive lock
    try:
        with pytest.raises(GraphBusy):
            GraphStore(path, attempts=3, backoff_seconds=0.01).read("MATCH (n) RETURN n")
    finally:
        holder.close()
    assert GraphStore(path).read("MATCH (n:Check) RETURN count(n) AS n") == [{"n": 1}]


def test_reads_are_bounded_not_truncated(tmp_path):
    store = GraphStore(tmp_path / "bound.rl", max_rows=5)
    with store.write() as tx:
        tx.run("UNWIND range(1, 10) AS i CREATE (:N {i: i})")
    assert len(store.read("MATCH (n:N) RETURN n.i AS i LIMIT 5")) == 5
    with pytest.raises(GraphTruncated):
        store.read("MATCH (n:N) RETURN n.i AS i")


def test_syntax_errors_surface(tmp_path):
    store = GraphStore(tmp_path / "s.rl")
    with store.write() as tx:
        tx.run("CREATE (:N)")
    with pytest.raises(GraphError, match="syntax"):
        store.read("MATCH (n RETURN n")


def test_bundled_demo_graph_matches_a_fresh_build(tmp_path):
    """data/demo.rl is committed; it must equal what the fixtures rebuild to."""
    assert BUNDLED_GRAPH.is_file()
    fresh = tmp_path / "fresh.rl"
    build_bundle(fresh)
    bundled_copy = tmp_path / "bundled.rl"
    shutil.copyfile(BUNDLED_GRAPH, bundled_copy)  # never open the committed file for writing
    for statement in (
        cypher.ALL_NODES,
        cypher.ALL_RELATIONSHIPS,
        cypher.SHOW_CONSTRAINTS,
        cypher.SHOW_INDEXES,
    ):
        assert GraphStore(bundled_copy).read(statement) == GraphStore(fresh).read(statement)


@pytest.mark.skipif(shutil.which("relate") is None, reason="relate CLI not on PATH")
def test_relate_cli_reads_the_bundled_store():
    out = subprocess.run(["relate", "--version"], capture_output=True, text=True, check=True).stdout
    assert "v1.0.1" in out
    result = subprocess.run(
        ["relate", "--database", str(BUNDLED_GRAPH), "--read-only", "--mode", "envelope",
         "MATCH (n) RETURN count(n) AS nodes"],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    assert '"nodes":68' in result.stdout.replace(" ", "")


def test_a_limit_parameter_cannot_hide_truncation(tmp_path):
    """The store binds $limit itself, so a caller cannot ask for fewer rows and get a short
    answer that looks complete."""
    statement = "MATCH (n:N) RETURN n.i AS i ORDER BY i LIMIT $limit"
    path = tmp_path / "limit.rl"
    with GraphStore(path).write() as tx:
        tx.run("UNWIND range(1, 10) AS i CREATE (:N {i: i})")
    with pytest.raises(GraphTruncated):
        GraphStore(path, max_rows=5).read(statement)
    with pytest.raises(GraphTruncated):
        GraphStore(path, max_rows=5).read(statement, {"limit": 5})  # the caller's value is ignored
    assert len(GraphStore(path, max_rows=10).read(statement)) == 10  # exactly at the bound is fine


def test_every_multi_row_read_statement_is_bounded():
    bounded = (
        "MATCH_CANDIDATES", "COVERAGE_GAPS", "EXCLUDED_FOR_VOLUNTEER", "OPTIONAL_SKILLS_EXCLUDED",
        "BROWSE_PROJECTS", "INSPECT", "ALL_NODES", "ALL_RELATIONSHIPS",
    )  # fmt: skip
    for name in bounded:
        assert getattr(cypher, name).rstrip().endswith("LIMIT $limit"), name


def test_a_read_gives_up_on_a_locked_store_within_about_a_second(tmp_path, monkeypatch):
    """A read waits while holding the in-process lock, so every other request queues behind it.
    It gives up quickly; a write keeps the longer window."""
    from app.graph import store as store_module

    path = tmp_path / "busy.rl"
    store = GraphStore(path)
    with store.write() as tx:
        tx.run("CREATE (:Check)")
    slept: list[float] = []
    monkeypatch.setattr(store_module.time, "sleep", slept.append)
    holder = relate.connect(str(path))
    try:
        with pytest.raises(GraphBusy):
            store.read("MATCH (n) RETURN n")
        waited_reading = sum(slept)
        slept.clear()
        with pytest.raises(GraphBusy), store.write():
            pass
    finally:
        holder.close()
    assert waited_reading <= 1.1 and store.read_attempts == 6
    assert sum(slept) > 10 and len(slept) == store.attempts == 40
