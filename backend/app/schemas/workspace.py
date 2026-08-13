"""Intake and response schemas for workspaces — Feature 1, sub-feature 1.

The same Pydantic models back the HTML form, the API contract and the agent inputs,
which is core_resoruces.md's stated reason for generating one shared JSON Schema:
a later run cannot silently reinterpret what `claimed_revenue` means.

Amounts arrive from the UI as decimal strings in major units ("10000000.50") and are
converted here, once, into integer minor units. Nothing downstream sees a float.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.money import MoneyError, format_money, to_minor_units

# Reporting periods longer than this are almost certainly a typo (e.g. year 2206),
# and would make every proration meaningless.
MAX_PERIOD_DAYS = 366 * 5


class WorkspaceCreate(BaseModel):
    """What a founder submits on the Workspace Setup screen (§10.1)."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    company_name: Annotated[str, Field(min_length=1, max_length=300)]
    legal_name: Annotated[str | None, Field(max_length=300)] = None

    reporting_period_start: date
    reporting_period_end: date
    base_currency: Annotated[str, Field(min_length=3, max_length=3)] = "INR"

    # Decimal strings in major units; converted to minor units by the validator.
    claimed_revenue: Decimal = Decimal("0")
    claimed_arr: Decimal = Decimal("0")

    materiality_threshold_pct: Annotated[float, Field(gt=0, le=100)] = 1.0
    accounting_method: Literal["accrual", "cash"] = "accrual"

    @field_validator("base_currency")
    @classmethod
    def _upper_currency(cls, value: str) -> str:
        code = value.strip().upper()
        if not code.isalpha():
            raise ValueError(f"invalid ISO-4217 currency code: {value!r}")
        return code

    @field_validator("claimed_revenue", "claimed_arr")
    @classmethod
    def _non_negative(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("claimed amounts cannot be negative")
        return value

    @model_validator(mode="after")
    def _validate_period(self) -> WorkspaceCreate:
        if self.reporting_period_end < self.reporting_period_start:
            raise ValueError(
                "reporting_period_end must be on or after reporting_period_start"
            )
        span = (self.reporting_period_end - self.reporting_period_start).days
        if span > MAX_PERIOD_DAYS:
            raise ValueError(
                f"reporting period spans {span} days, which exceeds the "
                f"{MAX_PERIOD_DAYS}-day maximum; check the dates"
            )
        # Verify the claims convert cleanly before anything is persisted.
        try:
            to_minor_units(self.claimed_revenue, self.base_currency)
            to_minor_units(self.claimed_arr, self.base_currency)
        except MoneyError as exc:
            raise ValueError(str(exc)) from exc
        return self

    def claimed_revenue_minor(self) -> int:
        return to_minor_units(self.claimed_revenue, self.base_currency)

    def claimed_arr_minor(self) -> int:
        return to_minor_units(self.claimed_arr, self.base_currency)


class WorkspaceUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    company_name: Annotated[str | None, Field(min_length=1, max_length=300)] = None
    legal_name: Annotated[str | None, Field(max_length=300)] = None
    claimed_revenue: Decimal | None = None
    claimed_arr: Decimal | None = None
    materiality_threshold_pct: Annotated[float | None, Field(gt=0, le=100)] = None


class MoneyOut(BaseModel):
    """How every amount crosses the API boundary.

    Carries minor units (exact, for computation), a decimal string (exact, for
    re-parsing) and a formatted string (for display). The UI never does money maths
    itself, so a JavaScript float can never corrupt a figure.
    """

    minor: int
    currency: str
    decimal: str
    display: str

    @classmethod
    def build(cls, minor: int, currency: str) -> MoneyOut:
        from app.core.money import from_minor_units

        return cls(
            minor=minor,
            currency=currency,
            decimal=str(from_minor_units(minor, currency)),
            display=f"{currency} {format_money(minor, currency)}",
        )


class WorkspaceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_name: str
    legal_name: str | None
    reporting_period_start: date
    reporting_period_end: date
    base_currency: str
    claimed_revenue: MoneyOut
    claimed_arr: MoneyOut
    materiality_threshold_pct: float
    accounting_method: str
    active_policy_version: str
    created_at: datetime

    @classmethod
    def from_model(cls, workspace: Any) -> WorkspaceOut:
        return cls(
            id=workspace.id,
            company_name=workspace.company_name,
            legal_name=workspace.legal_name,
            reporting_period_start=workspace.reporting_period_start,
            reporting_period_end=workspace.reporting_period_end,
            base_currency=workspace.base_currency,
            claimed_revenue=MoneyOut.build(workspace.claimed_revenue, workspace.base_currency),
            claimed_arr=MoneyOut.build(workspace.claimed_arr, workspace.base_currency),
            materiality_threshold_pct=float(workspace.materiality_threshold_pct),
            accounting_method=workspace.accounting_method,
            active_policy_version=workspace.active_policy_version,
            created_at=workspace.created_at,
        )


class WorkspaceSummary(BaseModel):
    """Dashboard header: the claim beside what the evidence currently supports."""

    workspace: WorkspaceOut
    evidence_counts: dict[str, int]
    connections: list[dict[str, Any]]
    #: Which providers this *deployment* can reach with its own credentials. This is
    #: capability, and is not the same question as `connections[].is_synthetic`,
    #: which records what the last fetch actually served. Conflating the two meant a
    #: machine with four live accounts offered no way to use them the moment a demo
    #: run had been performed.
    deployment_providers: dict[str, bool] = {}
    latest_run: dict[str, Any] | None
    open_review_items: int
    quarantined_records: int
