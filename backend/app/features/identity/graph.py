"""Evidence graph persistence and relationship discovery — Feature 2, sub-feature 4.

Neo4j stores *proposed and accepted* identity links together with the evidence
behind each one, plus the organisational and payment relationships that Feature 6
later investigates for related-party and circular-flow patterns.

Two rules run through every query here:

* **Workspace in every key and every match.** A graph traversal wanders by nature,
  which makes it the easiest place to leak across tenants. Every node carries
  `workspace_id` and every `MATCH` filters on it.
* **Bounded traversal.** Variable-length paths are capped. An unbounded traversal on
  a dense payment graph can enumerate an exponential number of paths and hang the
  request; core_resoruces.md is explicit that depth and relationship types must be
  bounded.

The graph never *decides* anything. Parameterised Cypher computes exact evidence
paths; the decisions live in PostgreSQL.
"""

from __future__ import annotations

from typing import Any

from app.core import graph_db
from app.core.events import EventKind, Severity, emit
from app.features.identity.matching import MatchCandidate


async def persist_source_records(
    workspace_id: str, records: list[dict[str, Any]]
) -> int:
    """Upsert one node per source record (invoice customer, CRM account, ...)."""
    if not records:
        return 0
    statements = [
        (
            """
            MERGE (n:SourceRecord {workspace_id: $workspace_id, id: $id})
            SET n.display_name = $display_name,
                n.source_system = $source_system,
                n.record_type = $record_type,
                n.normalized_name = $normalized_name
            """,
            {"workspace_id": workspace_id, **record},
        )
        for record in records
    ]
    await graph_db.execute_write(statements, workspace_id=workspace_id)
    return len(records)


async def persist_match_links(
    workspace_id: str, candidates: list[MatchCandidate]
) -> dict[str, int]:
    """Store every scored link with its decision, weight and evidence.

    Rejected pairs are stored too. "Why were these two *not* merged?" is a question
    reviewers ask constantly, and only a persisted negative can answer it.
    """
    statements: list[tuple[str, dict[str, Any]]] = []
    counts = {"ACCEPTED": 0, "REVIEW": 0, "REJECTED": 0}

    for candidate in candidates:
        decision = candidate.decision
        counts[decision] = counts.get(decision, 0) + 1
        statements.append(
            (
                """
                MATCH (a:SourceRecord {workspace_id: $workspace_id, id: $left})
                MATCH (b:SourceRecord {workspace_id: $workspace_id, id: $right})
                MERGE (a)-[r:MATCHES]->(b)
                SET r.decision = $decision,
                    r.weight = $weight,
                    r.probability = $probability,
                    r.method = $method,
                    r.blocking_rule = $blocking_rule,
                    r.evidence = $evidence,
                    r.workspace_id = $workspace_id
                """,
                {
                    "workspace_id": workspace_id,
                    "left": candidate.left.record_id,
                    "right": candidate.right.record_id,
                    "decision": decision,
                    "weight": round(candidate.total_weight, 3),
                    "probability": round(candidate.probability, 4),
                    "method": candidate.method,
                    "blocking_rule": candidate.blocking_rule,
                    # Neo4j properties must be primitives, so the evidence trail is
                    # flattened to strings rather than stored as nested maps.
                    "evidence": [
                        f"{item['field']}:{item['outcome']}:{item['weight']}"
                        for item in candidate.explain()
                    ],
                },
            )
        )

    if statements:
        await graph_db.execute_write(statements, workspace_id=workspace_id)
    emit(
        EventKind.PERSISTENCE,
        f"Evidence graph updated: {counts.get('ACCEPTED', 0)} accepted, "
        f"{counts.get('REVIEW', 0)} for review, {counts.get('REJECTED', 0)} rejected",
        workspace_id=workspace_id,
        feature=2,
        severity=Severity.SUCCESS,
    )
    return counts


async def persist_customer_clusters(
    workspace_id: str, clusters: dict[str, list[str]], names: dict[str, str]
) -> int:
    """Create a Customer node per cluster and attach its source records."""
    statements: list[tuple[str, dict[str, Any]]] = []
    for cluster_id, members in clusters.items():
        statements.append(
            (
                """
                MERGE (c:Customer {workspace_id: $workspace_id, id: $cluster_id})
                SET c.canonical_name = $name, c.member_count = $count
                """,
                {
                    "workspace_id": workspace_id,
                    "cluster_id": cluster_id,
                    "name": names.get(cluster_id, cluster_id),
                    "count": len(members),
                },
            )
        )
        for member in members:
            statements.append(
                (
                    """
                    MATCH (c:Customer {workspace_id: $workspace_id, id: $cluster_id})
                    MATCH (s:SourceRecord {workspace_id: $workspace_id, id: $member})
                    MERGE (c)-[:HAS_RECORD]->(s)
                    """,
                    {
                        "workspace_id": workspace_id,
                        "cluster_id": cluster_id,
                        "member": member,
                    },
                )
            )
    if statements:
        await graph_db.execute_write(statements, workspace_id=workspace_id)
    return len(clusters)


async def persist_shared_attributes(
    workspace_id: str,
    *,
    shared_domains: dict[str, list[str]],
    shared_addresses: dict[str, list[str]],
    shared_accounts: dict[str, list[str]],
) -> int:
    """Record entities sharing a domain, address or payment account.

    These are **observations, not conclusions**. Two companies at one address may be
    a parent and subsidiary, two tenants of a business centre, or a related party
    worth investigating. core_resoruces.md is explicit that a relationship flag
    cannot prove legal beneficial ownership, so the graph records what was observed
    and Feature 6 decides whether it is worth a reviewer's time.
    """
    statements: list[tuple[str, dict[str, Any]]] = []
    written = 0

    for relation, groups in (
        ("SHARES_DOMAIN", shared_domains),
        ("SHARES_ADDRESS", shared_addresses),
        ("SHARES_ACCOUNT", shared_accounts),
    ):
        for attribute, members in groups.items():
            unique = sorted(set(members))
            if len(unique) < 2:
                continue
            for index, left in enumerate(unique):
                for right in unique[index + 1 :]:
                    statements.append(
                        (
                            f"""
                            MATCH (a:Customer {{workspace_id: $workspace_id, id: $left}})
                            MATCH (b:Customer {{workspace_id: $workspace_id, id: $right}})
                            MERGE (a)-[r:{relation}]->(b)
                            SET r.attribute = $attribute, r.workspace_id = $workspace_id
                            """,
                            {
                                "workspace_id": workspace_id,
                                "left": left,
                                "right": right,
                                "attribute": attribute,
                            },
                        )
                    )
                    written += 1

    if statements:
        await graph_db.execute_write(statements, workspace_id=workspace_id)
    return written


async def find_related_entities(
    workspace_id: str, *, max_depth: int = 3
) -> list[dict[str, Any]]:
    """Customers connected by shared attributes, with the path that connects them.

    Depth is capped: an unbounded variable-length traversal over a dense graph can
    enumerate an exponential number of paths and never return.
    """
    depth = max(1, min(max_depth, 4))
    rows = await graph_db.execute(
        f"""
        MATCH path = (a:Customer {{workspace_id: $workspace_id}})
              -[:SHARES_DOMAIN|SHARES_ADDRESS|SHARES_ACCOUNT*1..{depth}]-
              (b:Customer {{workspace_id: $workspace_id}})
        WHERE a.id < b.id
        RETURN a.canonical_name AS source,
               b.canonical_name AS target,
               [rel IN relationships(path) | type(rel)] AS relationship_types,
               [rel IN relationships(path) | rel.attribute] AS attributes,
               length(path) AS hops
        ORDER BY hops ASC
        LIMIT 200
        """,
        {"workspace_id": workspace_id},
        workspace_id=workspace_id,
    )
    return rows


async def neighbourhood(
    workspace_id: str, node_id: str, *, max_depth: int = 2, limit: int = 100
) -> dict[str, Any]:
    """Bounded subgraph around one node, for the evidence-graph UI.

    Never returns the whole tenant graph — core_resoruces.md requires server-side
    neighbourhood limits so the browser is not handed every node in the workspace.
    """
    depth = max(1, min(max_depth, 3))
    rows = await graph_db.execute(
        f"""
        MATCH path = (start {{workspace_id: $workspace_id, id: $node_id}})
              -[*1..{depth}]-(other {{workspace_id: $workspace_id}})
        RETURN [n IN nodes(path) | {{
                    id: n.id,
                    label: coalesce(n.canonical_name, n.display_name, n.id),
                    kind: labels(n)[0]
                }}] AS nodes,
               [r IN relationships(path) | {{
                    type: type(r),
                    decision: r.decision,
                    weight: r.weight
                }}] AS edges
        LIMIT $limit
        """,
        {"workspace_id": workspace_id, "node_id": node_id, "limit": limit},
        workspace_id=workspace_id,
    )

    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    for row in rows:
        for node in row.get("nodes") or []:
            if node.get("id"):
                nodes[node["id"]] = node
        edges.extend(edge for edge in (row.get("edges") or []) if edge)
    return {"nodes": list(nodes.values()), "edges": edges}


async def clear_identity_graph(workspace_id: str) -> int:
    """Remove identity nodes so a re-run rebuilds rather than accumulating."""
    rows = await graph_db.execute(
        """
        MATCH (n {workspace_id: $workspace_id})
        WHERE n:SourceRecord OR n:Customer
        DETACH DELETE n
        RETURN count(n) AS deleted
        """,
        {"workspace_id": workspace_id},
        workspace_id=workspace_id,
    )
    return rows[0]["deleted"] if rows else 0
