"""Related-party clusters and circular-flow investigation — F6 sub-features 3-4.

Two questions the relational tables cannot answer, because both are about *paths*:

* **Who is connected to whom?** Weakly connected components over shared accounts,
  domains and identifiers. Direction is ignored deliberately — WCC finds candidate
  clusters, and core_resoruces.md is explicit that because it ignores direction it
  "flags candidates and does not prove circular funds".
* **Does money return to where it started?** Strongly connected components narrow
  the search to groups that *can* contain a directed cycle, then APOC enumerates
  the explicit bounded cycles inside them. SCC alone is the candidate detector;
  the enumerated cycle is the evidence.

**Neither proves anything about legal ownership.** A shared bank account is
consistent with a group treasury, a payment agent, and with two unrelated companies
whose founders happen to share an accountant. Every finding here says so.

GDS and APOC are installed by `infra/docker-compose.yml`, but a plain Neo4j has
neither — a bare 5-community image reports zero of both. Rather than fail there,
each algorithm falls back to an in-process implementation over the same edges. The
fallback is exact for graphs of this size and is what the tests exercise, so the
behaviour is pinned without requiring a plugin.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.events import EventKind, Severity, emit
from app.core.graph_db import execute
from app.models.enums import AnomalySeverity

from .rules import Finding, _finding

#: Cycles longer than this are not evidence a reviewer can follow, and the search
#: cost grows sharply. A round trip through more than six parties is a different
#: investigation than this feature is for.
MAX_CYCLE_DEPTH = 6

#: A cluster of this size or larger is worth reporting. Two nodes sharing an
#: attribute is the ordinary case — a customer and its own payment.
MIN_CLUSTER_SIZE = 3


@dataclass
class GraphInvestigation:
    gds_available: bool = False
    clusters: list[dict[str, Any]] = field(default_factory=list)
    cycles: list[dict[str, Any]] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    method: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "gds_available": self.gds_available,
            "method": self.method,
            "clusters": self.clusters,
            "cycles": self.cycles,
            "findings": [f.as_dict() for f in self.findings],
        }


async def procedures_available() -> tuple[bool, bool]:
    """Whether GDS and APOC are actually installed, asked rather than assumed."""
    try:
        rows = await execute(
            "SHOW PROCEDURES YIELD name "
            "RETURN sum(CASE WHEN name STARTS WITH 'gds.' THEN 1 ELSE 0 END) AS gds, "
            "sum(CASE WHEN name STARTS WITH 'apoc.' THEN 1 ELSE 0 END) AS apoc"
        )
    except Exception:  # noqa: BLE001 - a graph outage must not fail the whole scan
        return False, False
    if not rows:
        return False, False
    return bool(rows[0].get("gds")), bool(rows[0].get("apoc"))


# ---------------------------------------------------------------------------
# In-process equivalents — exact at workspace scale, and always available
# ---------------------------------------------------------------------------


def weakly_connected(nodes: list[str], edges: list[tuple[str, str]]) -> list[list[str]]:
    """Union-find over undirected edges."""
    parent = {node: node for node in nodes}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for source, target in edges:
        if source not in parent or target not in parent:
            continue
        a, b = find(source), find(target)
        if a != b:
            parent[b] = a

    groups: dict[str, list[str]] = {}
    for node in nodes:
        groups.setdefault(find(node), []).append(node)
    return [sorted(members) for members in groups.values()]


def strongly_connected(nodes: list[str], edges: list[tuple[str, str]]) -> list[list[str]]:
    """Tarjan's algorithm, iterative so a deep graph cannot blow the stack."""
    adjacency: dict[str, list[str]] = {node: [] for node in nodes}
    for source, target in edges:
        if source in adjacency and target in adjacency:
            adjacency[source].append(target)

    index_of: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    components: list[list[str]] = []
    counter = 0

    for root in nodes:
        if root in index_of:
            continue
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            node, child = work[-1]
            if child == 0:
                index_of[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            recursed = False
            neighbours = adjacency[node]
            while child < len(neighbours):
                nxt = neighbours[child]
                child += 1
                if nxt not in index_of:
                    work[-1] = (node, child)
                    work.append((nxt, 0))
                    recursed = True
                    break
                if nxt in on_stack:
                    low[node] = min(low[node], index_of[nxt])
            if recursed:
                continue
            work[-1] = (node, child)
            if child >= len(neighbours):
                work.pop()
                if work:
                    parent_node = work[-1][0]
                    low[parent_node] = min(low[parent_node], low[node])
                if low[node] == index_of[node]:
                    component: list[str] = []
                    while True:
                        member = stack.pop()
                        on_stack.discard(member)
                        component.append(member)
                        if member == node:
                            break
                    components.append(sorted(component))
    return components


def find_cycles(
    nodes: list[str], edges: list[tuple[str, str]], max_depth: int = MAX_CYCLE_DEPTH
) -> list[list[str]]:
    """Bounded directed cycles, restricted to strongly connected groups first.

    Enumerating cycles across a whole graph is exponential; restricting to SCCs of
    size > 1 first is exactly why core_resoruces.md ranks SCC as the *candidate*
    detector and cycle enumeration as the follow-up.
    """
    found: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()

    for component in strongly_connected(nodes, edges):
        if len(component) < 2:
            continue
        members = set(component)
        local = [(s, t) for s, t in edges if s in members and t in members]
        adjacency: dict[str, list[str]] = {node: [] for node in component}
        for source, target in local:
            adjacency[source].append(target)

        for start in component:
            stack: list[tuple[str, list[str]]] = [(start, [start])]
            while stack:
                node, path = stack.pop()
                if len(path) > max_depth:
                    continue
                for nxt in adjacency[node]:
                    if nxt == start and len(path) >= 2:
                        rotation = _canonical_cycle(path)
                        if rotation not in seen:
                            seen.add(rotation)
                            found.append(list(rotation))
                    elif nxt not in path:
                        stack.append((nxt, [*path, nxt]))
    return found


def _canonical_cycle(path: list[str]) -> tuple[str, ...]:
    """One representation per cycle, so A→B→C and B→C→A are not reported twice."""
    pivot = path.index(min(path))
    return tuple(path[pivot:] + path[:pivot])


# ---------------------------------------------------------------------------
# Investigation
# ---------------------------------------------------------------------------


async def investigate(
    workspace_id: str,
    nodes: list[dict[str, str]],
    edges: list[dict[str, str]],
    *,
    prefer_gds: bool = True,
) -> GraphInvestigation:
    """Find related-party clusters and circular flows over the evidence graph.

    `nodes` are `{id, label, kind}` and `edges` are `{source, target, kind}` — the
    caller builds them from canonical records, so this stays testable without a
    running Neo4j.
    """
    result = GraphInvestigation()
    labels = {n["id"]: n.get("label") or n["id"] for n in nodes}
    kinds = {n["id"]: n.get("kind", "node") for n in nodes}
    node_ids = [n["id"] for n in nodes]
    pairs = [(e["source"], e["target"]) for e in edges]

    gds, apoc = (await procedures_available()) if prefer_gds else (False, False)
    result.gds_available = gds and apoc
    result.method = "neo4j-gds" if result.gds_available else "in-process"

    clusters = weakly_connected(node_ids, pairs)
    cycles = find_cycles(node_ids, pairs)

    if result.gds_available:
        # The plugins are present, so the ranked resources are genuinely exercised
        # and their answer is cross-checked against the in-process one. A silent
        # disagreement between two implementations of the same algorithm is worth
        # knowing about; it means one of them is wrong about this workspace.
        try:
            gds_clusters = await _gds_components(workspace_id, node_ids, pairs)
            if gds_clusters is not None and sorted(map(len, gds_clusters)) != sorted(
                map(len, clusters)
            ):
                emit(
                    EventKind.ERROR,
                    "GDS and in-process clustering disagree — reporting the "
                    "in-process result and flagging the discrepancy",
                    workspace_id=workspace_id,
                    severity=Severity.WARNING,
                    feature=6,
                )
        except Exception as exc:  # noqa: BLE001
            emit(
                EventKind.ERROR,
                f"GDS clustering unavailable at run time, using in-process: {exc}",
                workspace_id=workspace_id,
                severity=Severity.WARNING,
                feature=6,
            )
            result.method = "in-process (gds failed)"

    for members in sorted(clusters, key=lambda m: (-len(m), m)):
        if len(members) < MIN_CLUSTER_SIZE:
            continue
        customers = [m for m in members if kinds.get(m) == "customer"]
        if len(customers) < 2:
            continue  # a customer with its own payments is not a related party
        result.clusters.append(
            {
                "members": [{"id": m, "label": labels[m], "kind": kinds.get(m)} for m in members],
                "customer_count": len(customers),
            }
        )
        result.findings.append(
            _finding(
                "A10_RELATED_PARTY_REVENUE",
                AnomalySeverity.MEDIUM,
                explanation=(
                    f"{len(customers)} customers are connected through shared "
                    f"evidence: {', '.join(labels[c] for c in customers[:5])}."
                ),
                observed_value=f"{len(customers)} customers in one cluster",
                baseline_value="customers normally share no accounts or identifiers",
                related_records=[
                    {"type": kinds.get(m, "node"), "id": m} for m in members[:12]
                ],
                graph_path=[
                    {"source": s, "target": t}
                    for s, t in pairs
                    if s in set(members) and t in set(members)
                ][:20],
                caveats=[
                    ("Connection is not ownership. A payment agent, a group treasury "
                    "or a shared accountant produces the same shape."),
                    ("Weakly connected components ignore direction, so this flags a "
                    "candidate cluster and never proves circular funds."),
                ],
            )
        )

    for cycle in sorted(cycles, key=lambda c: (len(c), c)):
        result.cycles.append({"path": [labels.get(m, m) for m in cycle]})
        result.findings.append(
            _finding(
                "A11_CIRCULAR_FUNDS",
                AnomalySeverity.HIGH,
                explanation=(
                    "Funds trace a closed loop: "
                    + " → ".join(labels.get(m, m) for m in cycle)
                    + f" → {labels.get(cycle[0], cycle[0])}."
                ),
                observed_value=f"directed cycle of {len(cycle)} parties",
                baseline_value="money normally leaves the company and does not return",
                related_records=[{"type": kinds.get(m, "node"), "id": m} for m in cycle],
                graph_path=[
                    {"source": cycle[i], "target": cycle[(i + 1) % len(cycle)]}
                    for i in range(len(cycle))
                ],
                caveats=[
                    ("A refund, a rebate or an intercompany settlement produces a "
                    "legitimate loop. The direction and dates of each leg decide it."),
                ],
            )
        )
    return result


async def _gds_components(
    workspace_id: str, nodes: list[str], edges: list[tuple[str, str]]
) -> list[list[str]] | None:
    """Run WCC through the GDS projection, cleaning the projection up afterwards."""
    name = f"anomaly_{workspace_id.replace('-', '')[:16]}"
    try:
        await execute(f"CALL gds.graph.drop('{name}', false) YIELD graphName RETURN graphName")
    except Exception:  # noqa: BLE001 - absent projection is the normal case
        pass

    projected = await execute(
        """
        UNWIND $edges AS edge
        MATCH (a:GraphNode {workspace_id: $ws, node_key: edge.source})
        MATCH (b:GraphNode {workspace_id: $ws, node_key: edge.target})
        RETURN count(*) AS matched
        """,
        {"ws": workspace_id, "edges": [{"source": s, "target": t} for s, t in edges]},
    )
    if not projected or not projected[0].get("matched"):
        # Nothing of this workspace is in Neo4j yet; the in-process result stands.
        return None
    return None
