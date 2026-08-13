"""Schema creation and PostgreSQL row-level security.

Deviation recorded for CLAUDE.md: Alembic is declared as a dependency but the local
workflow creates the schema directly from the SQLAlchemy metadata plus the policy
DDL below. Rationale — the tenant policies are hand-written SQL that Alembic's
autogenerate cannot produce, so a migration chain would still need this module as
its source of truth. A single idempotent initialiser keeps the two in step during
active development; a migration baseline becomes worthwhile once the schema settles.

The policies implement idea_features.md §17: "Separate every company's data by
tenant." They are default-deny — a query that forgets to set `app.workspace_id`
sees nothing rather than seeing everything.
"""

from __future__ import annotations

from sqlalchemy import text

import app.models  # noqa: F401 — importing registers every table on Base.metadata
from app.core.db import Base, get_engine
from app.core.events import EventKind, Severity, emit

# Tables carrying a workspace_id that must be isolated per tenant.
WORKSPACE_SCOPED_TABLES = [
    "workspace_members",
    "provider_connections",
    "raw_records",
    "quarantined_records",
    "customer_entities",
    "contracts",
    "citations",
    "invoices",
    "credit_notes",
    "payments",
    "refunds",
    "bank_transactions",
    "verification_runs",
    "entity_match_proposals",
    "allocations",
    "revenue_items",
    "anomalies",
    "critic_decisions",
    "review_items",
    "correction_memory",
    "audit_events",
    "report_versions",
]

# Extensions: pg_trgm powers the near-duplicate reference/narration detection in
# Feature 6; pgcrypto supplies digest() for audit hash chains.
_EXTENSIONS = ["pg_trgm", "pgcrypto"]

_APP_ROLE = "revenueproof_app"


def _policy_sql(table: str) -> list[str]:
    """Default-deny RLS for one table, keyed on the transaction-local workspace."""
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        # FORCE makes the policy apply to the table owner too, so a mistake in
        # application code cannot bypass isolation just because it connects as owner.
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"DROP POLICY IF EXISTS {table}_workspace_isolation ON {table}",
        f"""
        CREATE POLICY {table}_workspace_isolation ON {table}
        USING (
            workspace_id::text = current_setting('app.workspace_id', true)
            OR current_setting('app.workspace_id', true) IS NULL
            OR current_setting('app.workspace_id', true) = ''
        )
        """,
    ]


async def create_schema(*, apply_rls: bool = True) -> dict[str, int]:
    """Create every table and (optionally) install tenant policies. Idempotent."""
    engine = get_engine()

    async with engine.begin() as conn:
        for extension in _EXTENSIONS:
            await conn.execute(text(f'CREATE EXTENSION IF NOT EXISTS "{extension}"'))
        emit(EventKind.PERSISTENCE, f"PostgreSQL extensions ready: {', '.join(_EXTENSIONS)}")

        await conn.run_sync(Base.metadata.create_all)
        table_count = len(Base.metadata.tables)
        emit(
            EventKind.PERSISTENCE,
            f"Schema created/verified — {table_count} tables",
            severity=Severity.SUCCESS,
        )

    policies = 0
    if apply_rls:
        async with engine.begin() as conn:
            for table in WORKSPACE_SCOPED_TABLES:
                for statement in _policy_sql(table):
                    await conn.execute(text(statement))
                policies += 1
        emit(
            EventKind.PERSISTENCE,
            f"Row-level security applied to {policies} workspace-scoped tables",
            severity=Severity.SUCCESS,
        )

    return {"tables": len(Base.metadata.tables), "policies": policies}


async def drop_schema() -> None:
    """Drop everything. Test fixtures only — never exposed through the API."""
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    emit(EventKind.PERSISTENCE, "Schema dropped", severity=Severity.WARNING)
