"""Shared column types and mixins for canonical records.

Two conventions apply to every table:

* **Workspace scoping.** Financial evidence for one company must never be visible
  to another. `WorkspaceScopedMixin` makes `workspace_id` non-optional and indexed
  so both the ORM filters and the row-security policies have something to bind to.
* **Money as integers.** Amounts are stored as `BIGINT` minor units alongside their
  currency, never as `NUMERIC` or `FLOAT`. See `app.core.money` for the rationale.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from app.core.db import Base


def new_id() -> uuid.UUID:
    return uuid.uuid4()


def utcnow() -> datetime:
    return datetime.now(UTC)


class UUIDPrimaryKeyMixin:
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_id
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class WorkspaceScopedMixin:
    """Every evidence and decision row belongs to exactly one workspace."""

    @declared_attr
    @classmethod
    def workspace_id(cls) -> Mapped[uuid.UUID]:
        return mapped_column(
            UUID(as_uuid=True),
            ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )


class MoneyColumns:
    """Helper producing a paired (minor-units, currency) column set.

    Used as `**MoneyColumns.pair("total")` inside a model definition so that an
    amount can never be declared without its currency.
    """

    @staticmethod
    def amount(name: str, nullable: bool = False) -> Mapped[int]:
        return mapped_column(BigInteger, name, nullable=nullable, default=0 if not nullable else None)

    @staticmethod
    def currency(name: str = "currency", nullable: bool = False) -> Mapped[str]:
        return mapped_column(String(3), name, nullable=nullable)


def money_column(nullable: bool = False, default: int | None = 0) -> Mapped[int]:
    """A BIGINT column holding integer minor units."""
    return mapped_column(BigInteger, nullable=nullable, default=default)


def currency_column(nullable: bool = False, default: str | None = None) -> Mapped[str]:
    """A 3-character ISO-4217 code."""
    return mapped_column(String(3), nullable=nullable, default=default)


def jsonb_column(nullable: bool = False, default: Any = dict) -> Mapped[dict]:
    return mapped_column(JSONB, nullable=nullable, default=default)


__all__ = [
    "Base",
    "BigInteger",
    "CheckConstraint",
    "Index",
    "JSONB",
    "MoneyColumns",
    "TimestampMixin",
    "UUID",
    "UUIDPrimaryKeyMixin",
    "WorkspaceScopedMixin",
    "currency_column",
    "jsonb_column",
    "money_column",
    "new_id",
    "utcnow",
]
