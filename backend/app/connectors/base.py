"""Connector protocol — Feature 1, sub-features 2 and 3.

Every evidence source implements `Connector.fetch()`, returning raw provider
payloads. Connectors do exactly one job: **retrieve and hand over what the provider
said**. idea_features.md §6.2 states the rule directly — "the agent may retrieve and
normalize data, but it cannot decide whether the data proves revenue."

Two behaviours are built into the base class rather than left to each provider:

* **Synthetic fallback.** With no credential, a connector serves the §15 dataset and
  marks the connection `is_synthetic`. The UI shows that badge, so a demo can never
  be mistaken for a real reconciliation. This is what lets the pipeline be built and
  tested end to end before any provider approval arrives.
* **Cursor-based incremental sync.** Each fetch returns the cursor to resume from, so
  a second run collects only what changed instead of refetching a full history.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.events import EventKind, Severity, emit
from app.models.enums import RecordType, SourceSystem


@dataclass
class FetchedRecord:
    """One raw provider payload, plus what is needed to vault it."""

    record_type: RecordType
    source_id: str
    payload: dict[str, Any]
    # Populated only for file-backed evidence (contracts from Drive).
    file_bytes: bytes | None = None
    file_name: str | None = None
    mime_type: str | None = None


@dataclass
class FetchResult:
    """Outcome of one connector run."""

    records: list[FetchedRecord] = field(default_factory=list)
    # Provider-specific resume token: a Drive page token, a Zoho page number, a
    # Razorpay `created_at` bound. Opaque to everything except its own connector.
    cursor: dict[str, Any] = field(default_factory=dict)
    is_synthetic: bool = False
    errors: list[str] = field(default_factory=list)
    # True when the provider signalled more data than one run collected.
    has_more: bool = False

    def count_by_type(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self.records:
            counts[str(record.record_type)] = counts.get(str(record.record_type), 0) + 1
        return counts


class ConnectorError(RuntimeError):
    """Retrieval failed. The connection is marked unhealthy; the run continues."""


class Connector(abc.ABC):
    """Base class for every evidence source."""

    source_system: SourceSystem
    #: Human-readable name shown in the connection list.
    display_name: str

    #: Set to serve the §15 dataset even when a real credential exists. Demonstrating
    #: the product must never require reaching into a founder's live accounts, and the
    #: §15 dataset is where the adversarial cases live — so the choice has to be
    #: explicit rather than implied by whichever keys happen to be in the environment.
    force_synthetic: bool = False

    def __init__(
        self,
        workspace_id: str,
        *,
        cursor: dict[str, Any] | None = None,
        force_synthetic: bool = False,
    ) -> None:
        self.workspace_id = workspace_id
        self.cursor = cursor or {}
        self.force_synthetic = force_synthetic

    # -- to implement -------------------------------------------------------

    @abc.abstractmethod
    def has_credentials(self) -> bool:
        """True when a real credential is configured for this source."""

    @abc.abstractmethod
    async def fetch_live(self) -> FetchResult:
        """Retrieve from the real provider API."""

    @abc.abstractmethod
    def fetch_synthetic(self) -> FetchResult:
        """Serve the spec §15 demonstration dataset."""

    # -- shared behaviour ---------------------------------------------------

    async def fetch(self) -> FetchResult:
        """Retrieve evidence, falling back to synthetic data when unconfigured."""
        if self.force_synthetic or not self.has_credentials():
            reason = (
                "demonstration mode requested"
                if self.force_synthetic
                else "no credentials"
            )
            emit(
                EventKind.API_CALL,
                f"{self.display_name}: {reason} — serving synthetic dataset",
                workspace_id=self.workspace_id,
                severity=Severity.WARNING,
                feature=1,
                source=str(self.source_system),
            )
            result = self.fetch_synthetic()
            result.is_synthetic = True
            emit(
                EventKind.RESULT,
                f"{self.display_name}: {len(result.records)} synthetic records",
                workspace_id=self.workspace_id,
                severity=Severity.INFO,
                feature=1,
                breakdown=result.count_by_type(),
            )
            return result

        started = datetime.now(UTC)
        emit(
            EventKind.API_CALL,
            f"→ {self.display_name}: fetching from live API",
            workspace_id=self.workspace_id,
            severity=Severity.DEBUG,
            feature=1,
            cursor=self.cursor,
        )
        try:
            result = await self.fetch_live()
        except httpx.HTTPStatusError as exc:
            raise ConnectorError(
                f"{self.display_name} returned HTTP {exc.response.status_code}: "
                f"{exc.response.text[:200]}"
            ) from exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ConnectorError(f"{self.display_name} unreachable: {exc}") from exc

        elapsed = (datetime.now(UTC) - started).total_seconds() * 1000
        emit(
            EventKind.RESULT,
            f"✓ {self.display_name}: {len(result.records)} records from live API",
            workspace_id=self.workspace_id,
            severity=Severity.SUCCESS,
            feature=1,
            duration_ms=elapsed,
            breakdown=result.count_by_type(),
            has_more=result.has_more,
        )
        return result


async def paginate(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    params: dict[str, Any],
    items_key: str,
    page_param: str = "page",
    max_pages: int = 50,
    workspace_id: str = "_system",
    label: str = "provider",
) -> list[dict[str, Any]]:
    """Walk a paginated provider endpoint.

    `max_pages` is a deliberate stop: a pagination bug that never terminates would
    otherwise hammer the provider and exhaust a free-tier quota. Hitting the cap is
    reported rather than silently truncating the evidence.
    """
    collected: list[dict[str, Any]] = []
    page = 1
    while page <= max_pages:
        response = await client.get(
            url, headers=headers, params={**params, page_param: page}
        )
        response.raise_for_status()
        body = response.json()
        items = body.get(items_key) or []
        collected.extend(items)

        emit(
            EventKind.API_CALL,
            f"{label}: page {page} → {len(items)} items",
            workspace_id=workspace_id,
            severity=Severity.DEBUG,
            feature=1,
            total_so_far=len(collected),
        )

        context = body.get("page_context") or {}
        more = context.get("has_more_page")
        if more is None:
            more = len(items) > 0 and len(items) >= int(params.get("per_page", 100) or 100)
        if not more:
            break
        page += 1

    if page > max_pages:
        emit(
            EventKind.ERROR,
            f"{label}: stopped at the {max_pages}-page safety limit; evidence may be incomplete",
            workspace_id=workspace_id,
            severity=Severity.WARNING,
            feature=1,
        )
    return collected
