"""Neo4j access for the evidence graph (Features 2, 6 and 8).

Division of labour, per core_resoruces.md's "Graph facts" ranking: parameterized
Cypher computes exact evidence paths and is authoritative; GraphRAG-style retrieval
is only ever used to help phrase an explanation. Nothing financial is decided from
a similarity search.

Every node key includes `workspace_id`, and every query filters on it. A graph is
the easiest place to accidentally leak across tenants because traversals wander.
"""

from __future__ import annotations

from typing import Any

from neo4j import AsyncDriver, AsyncGraphDatabase

from app.core.config import settings
from app.core.events import EventKind, Severity, emit

_driver: AsyncDriver | None = None

# Uniqueness keyed on (workspace_id, id) so two workspaces can hold the same
# customer identifier without colliding or merging.
_CONSTRAINTS = [
    "CREATE CONSTRAINT customer_key IF NOT EXISTS "
    "FOR (n:Customer) REQUIRE (n.workspace_id, n.id) IS UNIQUE",
    "CREATE CONSTRAINT contract_key IF NOT EXISTS "
    "FOR (n:Contract) REQUIRE (n.workspace_id, n.id) IS UNIQUE",
    "CREATE CONSTRAINT invoice_key IF NOT EXISTS "
    "FOR (n:Invoice) REQUIRE (n.workspace_id, n.id) IS UNIQUE",
    "CREATE CONSTRAINT payment_key IF NOT EXISTS "
    "FOR (n:Payment) REQUIRE (n.workspace_id, n.id) IS UNIQUE",
    "CREATE CONSTRAINT bank_txn_key IF NOT EXISTS "
    "FOR (n:BankTransaction) REQUIRE (n.workspace_id, n.id) IS UNIQUE",
    "CREATE CONSTRAINT refund_key IF NOT EXISTS "
    "FOR (n:Refund) REQUIRE (n.workspace_id, n.id) IS UNIQUE",
    "CREATE CONSTRAINT account_key IF NOT EXISTS "
    "FOR (n:Account) REQUIRE (n.workspace_id, n.id) IS UNIQUE",
]

_INDEXES = [
    "CREATE INDEX customer_workspace IF NOT EXISTS FOR (n:Customer) ON (n.workspace_id)",
    "CREATE INDEX payment_workspace IF NOT EXISTS FOR (n:Payment) ON (n.workspace_id)",
    "CREATE INDEX invoice_workspace IF NOT EXISTS FOR (n:Invoice) ON (n.workspace_id)",
]


def get_driver() -> AsyncDriver:
    global _driver
    if _driver is None:
        _driver = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_username, settings.neo4j_password),
            max_connection_pool_size=20,
        )
    return _driver


async def execute(
    query: str,
    parameters: dict[str, Any] | None = None,
    *,
    workspace_id: str | None = None,
) -> list[dict[str, Any]]:
    """Run a parameterized Cypher query and return plain dictionaries.

    Parameters are never interpolated into the query string — a customer named
    `Northstar" OR 1=1` is a realistic input, not a hypothetical.
    """
    driver = get_driver()
    params = parameters or {}
    async with driver.session(database=settings.neo4j_database) as session:
        result = await session.run(query, params)  # type: ignore[arg-type]
        records = [record.data() async for record in result]
    if workspace_id:
        emit(
            EventKind.PERSISTENCE,
            f"Neo4j query returned {len(records)} rows",
            workspace_id=workspace_id,
            severity=Severity.DEBUG,
            query=query.strip().split("\n")[0][:120],
        )
    return records


async def execute_write(
    statements: list[tuple[str, dict[str, Any]]],
    *,
    workspace_id: str | None = None,
) -> None:
    """Apply several statements in one ACID transaction.

    A cluster merge and its history event must commit together, or a rollback
    would leave the graph disagreeing with PostgreSQL about who a customer is.
    """
    driver = get_driver()
    async with driver.session(database=settings.neo4j_database) as session:

        async def _work(tx):
            for query, params in statements:
                await tx.run(query, params)

        await session.execute_write(_work)

    if workspace_id:
        emit(
            EventKind.PERSISTENCE,
            f"Neo4j write committed ({len(statements)} statements)",
            workspace_id=workspace_id,
            severity=Severity.DEBUG,
        )


async def ensure_constraints() -> int:
    """Install uniqueness constraints and indexes. Idempotent."""
    applied = 0
    for statement in _CONSTRAINTS + _INDEXES:
        try:
            await execute(statement)
            applied += 1
        except Exception as exc:  # pragma: no cover - depends on server edition
            emit(
                EventKind.ERROR,
                f"Neo4j constraint failed: {exc}",
                severity=Severity.WARNING,
                statement=statement[:100],
            )
    emit(
        EventKind.PERSISTENCE,
        f"Neo4j constraints/indexes ready ({applied}/{len(_CONSTRAINTS) + len(_INDEXES)})",
        severity=Severity.SUCCESS,
    )
    return applied


async def clear_workspace(workspace_id: str) -> int:
    """Delete a workspace's subgraph — used by the retention/deletion control."""
    rows = await execute(
        """
        MATCH (n {workspace_id: $workspace_id})
        DETACH DELETE n
        RETURN count(n) AS deleted
        """,
        {"workspace_id": workspace_id},
    )
    return rows[0]["deleted"] if rows else 0


async def healthcheck() -> dict[str, Any]:
    try:
        rows = await execute("RETURN 1 AS ok")
        return {"ok": bool(rows), "uri": settings.neo4j_uri}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200], "uri": settings.neo4j_uri}


async def close_driver() -> None:
    global _driver
    if _driver is not None:
        await _driver.close()
    _driver = None
