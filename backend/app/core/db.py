"""SQLAlchemy engine, session management and workspace scoping.

PostgreSQL is the system of record. Every financial fact lives here; Redis, Neo4j
and the WebSocket stream are derived or transient views of it.

Tenant isolation is enforced in two layers:
  * Application layer — `workspace_id` is a required column and every query path
    filters on it (see `app.core.tenancy`).
  * Database layer — `SET LOCAL app.workspace_id` per transaction, which the row
    security policies in the migrations read via `current_setting()`.
The second layer exists because the first one is only as good as the developer
who wrote the last query.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import MetaData, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings
from app.core.events import EventKind, Severity, emit

# Explicit naming convention so Alembic autogenerate produces stable, reviewable
# constraint names instead of database-assigned ones.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            settings.database_url,
            echo=False,
            pool_pre_ping=True,  # survive Postgres restarts during development
            pool_size=10,
            max_overflow=20,
            # Reconciliation jobs hold connections while solving; a short timeout
            # surfaces contention as an error rather than a silent hang.
            pool_timeout=30,
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            get_engine(),
            expire_on_commit=False,  # keep ORM objects usable after commit
            autoflush=False,
        )
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session that rolls back on error."""
    async with get_sessionmaker()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def set_workspace_scope(session: AsyncSession, workspace_id: str) -> None:
    """Bind the transaction to one workspace for row-level security.

    `SET LOCAL` is transaction-scoped, so a pooled connection cannot leak the
    setting into an unrelated request.
    """
    await session.execute(
        text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
        {"workspace_id": str(workspace_id)},
    )


async def healthcheck() -> dict[str, Any]:
    """Confirm a real round trip to PostgreSQL."""
    try:
        async with get_engine().connect() as conn:
            version = (await conn.execute(text("SHOW server_version"))).scalar_one()
        return {"ok": True, "version": str(version)}
    except Exception as exc:
        emit(
            EventKind.ERROR,
            f"PostgreSQL health check failed: {exc}",
            severity=Severity.ERROR,
        )
        return {"ok": False, "error": str(exc)}


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
