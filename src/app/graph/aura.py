"""Optional AuraDB destination, invoked explicitly from the CLI only (never on a request path).

`push` copies the synthetic demo graph from a Relate store into a designated *dev* AuraDB
instance with the Neo4j Python driver; every node it writes carries `dvn_scope`, and a push
first deletes only nodes with that scope. `parity` runs the very same MATCH_CANDIDATES text on
both engines for every organisation and compares the normalised rows.
"""

from typing import Any

from app.config import Settings
from app.graph import cypher
from app.graph.store import GraphStore

SCOPE = "dvn-demo-v1"


class AuraNotConfigured(RuntimeError):
    pass


def _driver(settings: Settings):
    password = settings.aura_password.get_secret_value()
    if not settings.aura_uri.startswith("neo4j+s://") or not password:
        raise AuraNotConfigured(
            "Set AURA_URI (neo4j+s://...) and AURA_PASSWORD for a designated dev instance."
        )
    try:
        from neo4j import GraphDatabase
    except ModuleNotFoundError as error:
        raise AuraNotConfigured("Install the driver: uv sync --group aura") from error
    return GraphDatabase.driver(settings.aura_uri, auth=(settings.aura_username, password))


def _export(graph: GraphStore) -> tuple[list[dict], list[dict]]:
    with graph.reader() as tx:
        nodes = tx.read(cypher.ALL_NODES)
        rels = tx.read(cypher.ALL_RELATIONSHIPS)
    return nodes, rels


def _group(rels: list[dict]) -> dict[tuple[str, str, str], list[dict]]:
    """Relationships by (source label, type, target label), checked against the graph schema.

    Labels and types are interpolated into Cypher by `push`, so anything the schema in
    app.graph.cypher does not list is refused here, before a single statement has run.
    """
    by_type: dict[tuple[str, str, str], list[dict]] = {}
    for r in rels:
        shape = (r["from_label"], r["type"], r["to_label"])
        if cypher.RELATIONSHIPS.get(r["type"]) != (r["from_label"], r["to_label"]):
            raise ValueError("unexpected relationship {}-{}->{}".format(*shape))
        by_type.setdefault(shape, []).append({"a": r["from_id"], "b": r["to_id"]})
    return by_type


def push(settings: Settings, graph: GraphStore) -> dict[str, int]:
    nodes, rels = _export(graph)
    if unknown := {n["label"] for n in nodes} - set(cypher.LABELS):
        raise ValueError(f"unexpected labels {sorted(unknown)}")
    by_type = _group(rels)  # validated before anything is deleted from the destination
    with _driver(settings) as driver:
        db = settings.aura_database
        driver.execute_query(
            "MATCH (n {dvn_scope: $scope}) DETACH DELETE n", scope=SCOPE, database_=db
        )
        for label in cypher.LABELS:
            rows = [n["props"] for n in nodes if n["label"] == label]
            # Label names come from the fixed schema in app.graph.cypher, never from data.
            driver.execute_query(
                f"UNWIND $rows AS row MERGE (n:{label} {{id: row.id}}) "
                "SET n += row, n.dvn_scope = $scope",
                rows=rows, scope=SCOPE, database_=db,
            )  # fmt: skip
        for (src, rel, dst), rows in by_type.items():
            driver.execute_query(
                f"UNWIND $rows AS row MATCH (a:{src} {{id: row.a, dvn_scope: $scope}}) "
                f"MATCH (b:{dst} {{id: row.b, dvn_scope: $scope}}) MERGE (a)-[:{rel}]->(b)",
                rows=rows, scope=SCOPE, database_=db,
            )  # fmt: skip
    return {"nodes": len(nodes), "relationships": len(rels)}


def _normalise(rows: list[dict[str, Any]]) -> list[tuple]:
    out = []
    for r in rows:
        out.append(
            tuple((k, tuple(sorted(v)) if isinstance(v, list) else v) for k, v in sorted(r.items()))
        )
    return sorted(out)


def parity(settings: Settings, graph: GraphStore, org_ids: list[str]) -> dict[str, Any]:
    report: dict[str, Any] = {"organisations": {}, "match": True}
    with _driver(settings) as driver:
        for org in org_ids:
            params = cypher.match_params(orgs=[org])
            local = graph.read(cypher.MATCH_CANDIDATES, params)
            # The same bound the local store applies, so both sides answer the same question.
            records, _, _ = driver.execute_query(
                cypher.MATCH_CANDIDATES,
                params | {"limit": graph.max_rows + 1},
                database_=settings.aura_database,
            )
            remote = [rec.data() for rec in records]
            same = _normalise(local) == _normalise(remote)
            report["organisations"][org] = {"local": len(local), "aura": len(remote), "same": same}
            report["match"] = report["match"] and same
    return report
