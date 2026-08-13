"""Workspace, users and provider connections — Feature 1, sub-features 1 and 2."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import (
    Base,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    WorkspaceScopedMixin,
    currency_column,
    money_column,
)
from app.models.enums import SourceSystem, UserRole


class Workspace(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One company under review, for one reporting period.

    The claimed figures live here because they are *the claim being tested*, not an
    accepted fact (idea_features.md §8). Every downstream calculation compares
    evidence against these numbers.
    """

    __tablename__ = "workspaces"

    company_name: Mapped[str] = mapped_column(String(300), nullable=False)
    legal_name: Mapped[str | None] = mapped_column(String(300))

    reporting_period_start: Mapped[date] = mapped_column(Date, nullable=False)
    reporting_period_end: Mapped[date] = mapped_column(Date, nullable=False)
    base_currency: Mapped[str] = currency_column(default="INR")

    # Founder-reported claim, in minor units of base_currency.
    claimed_revenue: Mapped[int] = money_column(default=0)
    claimed_arr: Mapped[int] = money_column(default=0)

    # Discrepancies below this fraction of claimed revenue are not "material" and
    # so do not require critic agreement before publication.
    materiality_threshold_pct: Mapped[float] = mapped_column(
        Numeric(6, 3), nullable=False, default=1.0
    )
    accounting_method: Mapped[str] = mapped_column(String(20), nullable=False, default="accrual")

    # Which versioned revenue/ARR policy governs this workspace's calculations.
    active_policy_version: Mapped[str] = mapped_column(String(50), nullable=False, default="v1")

    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "reporting_period_end >= reporting_period_start",
            name="period_end_after_start",
        ),
        CheckConstraint("claimed_revenue >= 0", name="claimed_revenue_non_negative"),
        CheckConstraint("claimed_arr >= 0", name="claimed_arr_non_negative"),
        CheckConstraint(
            "materiality_threshold_pct > 0 AND materiality_threshold_pct <= 100",
            name="materiality_within_range",
        ),
        CheckConstraint(
            "accounting_method IN ('accrual', 'cash')", name="accounting_method_valid"
        ),
    )

    members: Mapped[list[WorkspaceMember]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )
    connections: Mapped[list[ProviderConnection]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )


class User(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A person who can sign in. Workspace access is granted per membership."""

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    full_name: Mapped[str | None] = mapped_column(String(200))
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    memberships: Mapped[list[WorkspaceMember]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class WorkspaceMember(Base, UUIDPrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """Grants one user a role inside one workspace (RBAC, idea_features.md §17)."""

    __tablename__ = "workspace_members"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[UserRole] = mapped_column(String(20), nullable=False, default=UserRole.REVIEWER)

    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", name="one_membership_per_user"),
    )

    workspace: Mapped[Workspace] = relationship(back_populates="members")
    user: Mapped[User] = relationship(back_populates="memberships")


class ProviderConnection(Base, UUIDPrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """A connected evidence source and its synchronisation health.

    Tokens are stored encrypted (`app.core.crypto`); this table never holds a
    plaintext secret. `sync_cursor` carries whatever the provider uses to resume —
    a Drive page token, a Zoho page number, a Razorpay timestamp — so backfills are
    incremental rather than repeatedly refetching the whole history.
    """

    __tablename__ = "provider_connections"

    source_system: Mapped[SourceSystem] = mapped_column(String(40), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(200))

    # Provider-side account identity, recorded so evidence can name its origin.
    external_account_id: Mapped[str | None] = mapped_column(String(200))

    encrypted_access_token: Mapped[str | None] = mapped_column(Text)
    encrypted_refresh_token: Mapped[str | None] = mapped_column(Text)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scopes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # True when running against a provider's test/sandbox environment.
    is_test_mode: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # True when no credential exists and the connector reads the synthetic dataset.
    is_synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_status: Mapped[str | None] = mapped_column(String(40))
    last_sync_error: Mapped[str | None] = mapped_column(Text)
    sync_cursor: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    records_imported: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "source_system", name="one_connection_per_source"
        ),
        Index("ix_connection_active", "workspace_id", "is_active"),
    )

    workspace: Mapped[Workspace] = relationship(back_populates="connections")
