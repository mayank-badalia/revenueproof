"""Downloadable artefacts — the evidence position as files a person can work with.

One self-contained HTML report is the right thing to email and the wrong thing to
*work* with. A reviewer who wants to sort 67 classified items by recognised amount,
or hand the anomaly list to a colleague, or diff this month against last, needs the
same figures as data — so every table the product renders is also available as CSV,
and the whole position as a single zip.

Three rules the formats share:

* **Money is a decimal string, never a float.** `1,04,00,000.00` renders and
  `10400000.00` re-parses; a spreadsheet that reads a float has already lost paise.
  Both columns are present, because a CSV opened in Excel is read by a person and
  parsed by a script and those want different things.
* **Every row says which rule produced it and what evidence is missing.** A figure
  without its reason is not reviewable, and the whole point of the export is that it
  can be checked away from the screen that produced it.
* **Withheld and disputed rows are exported too**, marked as such. A reviewer's most
  useful export is the one containing the things still to decide — leaving them out
  would make the download useful only after the work was already finished.
"""

from __future__ import annotations

import csv
import io
import json
import uuid
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.money import format_money, from_minor_units
from app.models import (
    Allocation,
    Anomaly,
    BankTransaction,
    Contract,
    CustomerEntity,
    Invoice,
    Payment,
    ReviewItem,
    RevenueItem,
    Workspace,
)
from app.models.enums import RevenueClass

from . import report as report_builder

#: Every artefact the product can hand over, in the order a reader would meet them.
#: The key is what the API accepts; the label is what the button says.
ARTIFACTS: tuple[tuple[str, str, str], ...] = (
    ("report", "Full report", "html"),
    ("summary", "Position summary", "csv"),
    ("revenue-items", "Classified revenue items", "csv"),
    ("anomalies", "Anomaly indicators", "csv"),
    ("review-queue", "Open decisions", "csv"),
    ("contracts", "Contract terms", "csv"),
    ("customers", "Resolved customers", "csv"),
    ("reconciliation", "Invoice settlement", "csv"),
    ("evidence", "Evidence inventory", "csv"),
)


def _money(minor: int | None, currency: str) -> str:
    """Display form. Grouped for a human; the exact form travels beside it.

    Routed through `core.money.format_money` rather than formatting here, so a CSV
    and the screen it was exported from cannot group the same figure differently.
    """
    if minor is None:
        return ""
    return f"{currency} {format_money(minor, currency)}"


def _exact(minor: int | None, currency: str) -> str:
    """Re-parseable form. No separators, no symbol, full precision."""
    if minor is None:
        return ""
    return f"{from_minor_units(minor, currency):.2f}"


def _slug(text: str) -> str:
    cleaned = "".join(c.lower() if c.isalnum() else "-" for c in (text or "workspace"))
    return "-".join(part for part in cleaned.split("-") if part) or "workspace"


def _csv(header: list[str], rows: list[list[Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue()


@dataclass
class Artifact:
    """One downloadable file: what it is called, what it is, and its bytes."""

    key: str
    filename: str
    media_type: str
    body: bytes

    @property
    def size(self) -> int:
        return len(self.body)


# ---------------------------------------------------------------------------
# The individual tables
# ---------------------------------------------------------------------------


async def _load(session: AsyncSession, model, workspace_id: uuid.UUID, order=None):
    query = select(model).where(model.workspace_id == workspace_id)
    if order is not None:
        query = query.order_by(order)
    return list((await session.execute(query)).scalars().all())


CLASS_LABEL = {
    RevenueClass.VERIFIED_RECURRING: "Verified recurring",
    RevenueClass.VERIFIED_ONE_TIME: "Verified one-time",
    RevenueClass.CONTRACTED_UNPAID: "Contracted, unbilled",
    RevenueClass.INVOICED_UNPAID: "Invoiced, unpaid",
    RevenueClass.REFUNDED_OR_REVERSED: "Refunded or reversed",
    RevenueClass.PAYMENT_WITHOUT_SUPPORT: "Cash without support",
    RevenueClass.UNSUPPORTED_CLAIM: "Unsupported claim",
    RevenueClass.HUMAN_REVIEW: "Awaiting human review",
}


async def _revenue_items_csv(
    session: AsyncSession, workspace_id: uuid.UUID, currency: str
) -> str:
    rows = await _load(
        session, RevenueItem, workspace_id, RevenueItem.recognized_amount.desc()
    )
    return _csv(
        [
            "item", "classification", "counts_as_verified", "is_recurring",
            "gross_display", "gross_exact", "recognised_display", "recognised_exact",
            "currency", "evidence_strength", "rule_id", "why",
            "missing_evidence", "material", "published", "status_for_reviewer",
            "critic_verdict", "human_decision", "period_start", "period_end",
            "policy_version", "evidence_ids",
        ],
        [
            [
                row.description,
                CLASS_LABEL.get(RevenueClass(row.classification), row.classification),
                "yes" if RevenueClass(row.classification).counts_as_verified else "no",
                "yes" if row.is_recurring else "no",
                _money(row.gross_amount, row.currency),
                _exact(row.gross_amount, row.currency),
                _money(row.recognized_amount, row.currency),
                _exact(row.recognized_amount, row.currency),
                row.currency,
                str(row.evidence_strength),
                row.rule_id,
                row.rule_explanation,
                "; ".join(row.missing_evidence or []),
                "yes" if row.is_material else "no",
                "yes" if row.is_published else "no",
                # The reviewer's question is never "is this published" but "is there
                # anything left for me to do about it".
                "published"
                if row.is_published
                else ("withheld — " + (row.critic_verdict or "not yet reviewed")),
                row.critic_verdict or "",
                row.human_decision or "",
                row.period_start.isoformat() if row.period_start else "",
                row.period_end.isoformat() if row.period_end else "",
                row.policy_version or "",
                "; ".join(str(e) for e in (row.evidence_ids or [])),
            ]
            for row in rows
        ],
    )


async def _anomalies_csv(
    session: AsyncSession, workspace_id: uuid.UUID, currency: str
) -> str:
    rows = await _load(session, Anomaly, workspace_id, Anomaly.rule_id)
    severity_rank = {"high": 0, "medium": 1, "low": 2, "info": 3}
    rows.sort(key=lambda a: (severity_rank.get(str(a.severity), 9), a.rule_id))
    return _csv(
        [
            "severity", "rule_id", "title", "observed", "baseline",
            "what_was_found", "what_to_check", "caveats", "status",
            "marked_false_positive", "model_version", "model_score",
        ],
        [
            [
                str(row.severity),
                row.rule_id,
                row.title,
                row.observed_value or "",
                row.baseline_value or "",
                row.explanation or "",
                row.required_check or "",
                "; ".join(row.caveats or []),
                str(row.status),
                "yes" if row.is_false_positive else "no",
                row.model_version or "",
                f"{row.model_score:.4f}" if row.model_score is not None else "",
            ]
            for row in rows
        ],
    )


async def _review_queue_csv(session: AsyncSession, workspace_id: uuid.UUID) -> str:
    rows = await _load(session, ReviewItem, workspace_id, ReviewItem.created_at)
    return _csv(
        [
            "status", "severity", "category", "question", "detail",
            "resolution", "reason", "raised_at", "resolved_at",
        ],
        [
            [
                str(row.status),
                str(row.severity or ""),
                str(row.category or ""),
                row.title or "",
                row.detail or "",
                row.resolution or "",
                row.resolution_reason or "",
                row.created_at.isoformat() if row.created_at else "",
                row.resolved_at.isoformat() if row.resolved_at else "",
            ]
            for row in rows
        ],
    )


async def _contracts_csv(
    session: AsyncSession, workspace_id: uuid.UUID, currency: str
) -> str:
    rows = await _load(session, Contract, workspace_id, Contract.document_name)
    return _csv(
        [
            "document", "customer_named_in_document", "term_start", "term_end",
            "billing_frequency", "currency",
            "recurring_display", "recurring_exact",
            "one_time_display", "one_time_exact",
            "future_period_display", "future_period_exact",
            "auto_renewal", "was_read", "extraction_confidence", "scanned_pdf",
            "ocr_applied", "pages", "unknown_fields", "needs_human_review",
            "review_reasons",
        ],
        [
            [
                row.document_name,
                row.stated_customer_name or "",
                row.start_date.isoformat() if row.start_date else "",
                row.end_date.isoformat() if row.end_date else "",
                str(row.billing_frequency or ""),
                row.currency or currency,
                _money(row.recurring_amount, row.currency or currency),
                _exact(row.recurring_amount, row.currency or currency),
                _money(row.one_time_amount, row.currency or currency),
                _exact(row.one_time_amount, row.currency or currency),
                _money(row.future_period_amount, row.currency or currency),
                _exact(row.future_period_amount, row.currency or currency),
                "yes" if row.auto_renewal else "no",
                # An unread contract is not a contract worth zero, and the export
                # has to keep that distinction as plainly as the screen does.
                "no — terms unknown"
                if "terms_not_yet_extracted" in (row.unknown_fields or [])
                else "yes",
                f"{row.extraction_confidence:.2f}"
                if row.extraction_confidence is not None
                else "",
                "yes" if row.is_scanned else "no",
                "yes" if row.ocr_applied else "no",
                row.page_count or "",
                "; ".join(row.unknown_fields or []),
                "yes" if row.needs_human_review else "no",
                "; ".join(row.review_reasons or []),
            ]
            for row in rows
        ],
    )


async def _customers_csv(session: AsyncSession, workspace_id: uuid.UUID) -> str:
    rows = await _load(
        session, CustomerEntity, workspace_id, CustomerEntity.canonical_name
    )
    return _csv(
        [
            "customer", "also_known_as", "tax_identifiers", "domains",
            "email_addresses", "addresses", "match_confidence",
            "confirmed_by_a_human", "related_party", "related_party_reasons",
        ],
        [
            [
                row.canonical_name,
                "; ".join(row.known_aliases or []),
                "; ".join(row.tax_identifiers or []),
                "; ".join(row.domains or []),
                "; ".join(row.email_addresses or []),
                "; ".join(row.addresses or []),
                f"{row.match_confidence:.2f}"
                if row.match_confidence is not None
                else "",
                "yes" if row.human_confirmed else "no",
                str(row.related_party_status or ""),
                "; ".join(row.related_party_reasons or []),
            ]
            for row in rows
        ],
    )


async def _reconciliation_csv(
    session: AsyncSession, workspace_id: uuid.UUID, currency: str
) -> str:
    """Per-invoice settlement, rebuilt from the persisted allocations."""
    invoices = await _load(session, Invoice, workspace_id, Invoice.invoice_number)
    allocations = await _load(session, Allocation, workspace_id)
    by_invoice: dict[str, list[Allocation]] = {}
    for allocation in allocations:
        if allocation.invoice_id:
            by_invoice.setdefault(str(allocation.invoice_id), []).append(allocation)

    rows = []
    for invoice in invoices:
        links = by_invoice.get(str(invoice.id), [])
        allocated = sum(a.allocated_amount for a in links if a.reversed_at is None)
        bank_backed = sum(
            a.allocated_amount
            for a in links
            if a.reversed_at is None and a.bank_transaction_id is not None
        )
        rows.append([
            invoice.invoice_number or str(invoice.id),
            invoice.stated_customer_name or "",
            str(invoice.status),
            invoice.issue_date.isoformat() if invoice.issue_date else "",
            _money(invoice.total, invoice.currency or currency),
            _exact(invoice.total, invoice.currency or currency),
            _money(allocated, invoice.currency or currency),
            _exact(allocated, invoice.currency or currency),
            _money(invoice.total - allocated, invoice.currency or currency),
            _exact(invoice.total - allocated, invoice.currency or currency),
            _money(bank_backed, invoice.currency or currency),
            "yes" if bank_backed > 0 else "no",
            len(links),
            "; ".join(sorted({a.rule_id or "" for a in links if a.rule_id})),
        ])
    return _csv(
        [
            "invoice", "customer", "status", "invoice_date",
            "invoiced_display", "invoiced_exact",
            "allocated_display", "allocated_exact",
            "outstanding_display", "outstanding_exact",
            "bank_confirmed_display", "independently_bank_confirmed",
            "payment_links", "match_rules",
        ],
        rows,
    )


async def _evidence_csv(
    session: AsyncSession, workspace_id: uuid.UUID, currency: str
) -> str:
    """One row per piece of raw evidence, so the input can be checked as easily as
    the output. A reviewer disputing a figure asks what it was built from first."""
    rows: list[list[Any]] = []
    for payment in await _load(session, Payment, workspace_id, Payment.source_id):
        rows.append([
            "payment", payment.source_id, str(payment.source_system),
            payment.stated_customer_name or "",
            payment.payment_time.isoformat() if payment.payment_time else "",
            _money(payment.amount, payment.currency or currency),
            _exact(payment.amount, payment.currency or currency),
            str(payment.status), payment.reference or payment.description or "",
        ])
    for bank in await _load(
        session, BankTransaction, workspace_id, BankTransaction.transaction_date
    ):
        rows.append([
            "bank_transaction", bank.source_id, str(bank.source_system),
            bank.counterparty or "",
            bank.transaction_date.isoformat() if bank.transaction_date else "",
            _money(bank.amount, bank.currency or currency),
            _exact(bank.amount, bank.currency or currency),
            str(bank.direction), bank.reference or bank.narration or "",
        ])
    return _csv(
        [
            "kind", "source_id", "source_system", "counterparty", "date",
            "amount_display", "amount_exact", "status_or_direction", "reference",
        ],
        rows,
    )


async def _summary_csv(
    session: AsyncSession, workspace_id: uuid.UUID, workspace: Workspace
) -> str:
    """The headline position: what was claimed, what is proven, and the gap.

    Deliberately the first file in the bundle and the first a spreadsheet opens.
    """
    from app.features.room.versions import _snapshot

    currency = workspace.base_currency
    snapshot = await _snapshot(session, workspace_id=workspace_id)
    proven = snapshot["verified_recurring"] + snapshot["verified_one_time"]
    claimed = snapshot["claimed_revenue"]
    gap = claimed - proven
    rows = [
        ["Company", workspace.company_name, ""],
        ["Reporting period",
         f"{workspace.reporting_period_start} to {workspace.reporting_period_end}", ""],
        ["Currency", currency, ""],
        ["Accounting method", str(workspace.accounting_method or ""), ""],
        ["Policy version", snapshot["policy_version"], ""],
        ["", "", ""],
        ["Claimed revenue", _money(claimed, currency), _exact(claimed, currency)],
        ["Proven by evidence", _money(proven, currency), _exact(proven, currency)],
        [
            "Evidence beyond the claim" if gap < 0 else "Claimed but not yet proven",
            _money(abs(gap), currency),
            _exact(abs(gap), currency),
        ],
        [
            "Share of claim proven",
            f"{proven / claimed * 100:.1f}%" if claimed else "",
            "",
        ],
        ["", "", ""],
        ["Verified recurring", _money(snapshot["verified_recurring"], currency),
         _exact(snapshot["verified_recurring"], currency)],
        ["Verified one-time", _money(snapshot["verified_one_time"], currency),
         _exact(snapshot["verified_one_time"], currency)],
        ["Supported ARR", _money(snapshot["supported_arr"], currency),
         _exact(snapshot["supported_arr"], currency)],
        ["Claimed ARR", _money(snapshot["claimed_arr"], currency),
         _exact(snapshot["claimed_arr"], currency)],
        ["Refunded or reversed", _money(snapshot["refunded_reversed"], currency),
         _exact(snapshot["refunded_reversed"], currency)],
        ["Contracted, unbilled", _money(snapshot["contracted_unpaid"], currency),
         _exact(snapshot["contracted_unpaid"], currency)],
        ["Invoiced, unpaid", _money(snapshot["invoiced_unpaid"], currency),
         _exact(snapshot["invoiced_unpaid"], currency)],
        ["Cash without support", _money(snapshot["unsupported"], currency),
         _exact(snapshot["unsupported"], currency)],
        ["", "", ""],
        ["Largest customer share",
         f"{snapshot['largest_customer_concentration_pct']}%"
         if snapshot["largest_customer_concentration_pct"] is not None else "", ""],
        ["HHI", f"{snapshot['hhi']}" if snapshot["hhi"] is not None else "", ""],
        ["Concentration measured over",
         f"{snapshot['concentration_basis']}, "
         f"{snapshot['concentration_customers']} customers", ""],
        ["", "", ""],
        ["Items published", snapshot["items_published"], ""],
        ["Items withheld pending review",
         snapshot["items_total"] - snapshot["items_published"], ""],
        ["Open decisions", snapshot["items_awaiting_review"], ""],
        ["Open anomaly indicators", snapshot["open_anomalies"], ""],
    ]
    return _csv(["measure", "value", "exact_value"], rows)


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


async def build_artifact(
    session: AsyncSession, *, workspace_id: uuid.UUID, key: str
) -> Artifact:
    """One named artefact. Raises `ValueError` for a key that is not on offer."""
    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        raise ValueError("workspace not found")
    currency = workspace.base_currency
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
    base = f"revenueproof-{_slug(workspace.company_name)}"

    if key == "report":
        filename, html = await report_builder.build_report(
            session, workspace_id=workspace_id
        )
        return Artifact(key, filename, "text/html; charset=utf-8", html.encode())

    builders = {
        "summary": _summary_csv,
        "revenue-items": _revenue_items_csv,
        "anomalies": _anomalies_csv,
        "review-queue": _review_queue_csv,
        "contracts": _contracts_csv,
        "customers": _customers_csv,
        "reconciliation": _reconciliation_csv,
        "evidence": _evidence_csv,
    }
    if key not in builders:
        raise ValueError(f"unknown artefact {key!r}")

    builder = builders[key]
    if key == "summary":
        body = await builder(session, workspace_id, workspace)
    elif key in {"review-queue", "customers"}:
        body = await builder(session, workspace_id)
    else:
        body = await builder(session, workspace_id, currency)

    return Artifact(
        key,
        f"{base}-{key}-{stamp}.csv",
        "text/csv; charset=utf-8",
        # A BOM so Excel opens UTF-8 correctly; without it ₹ arrives as mojibake and
        # the reader concludes the export is broken.
        b"\xef\xbb\xbf" + body.encode(),
    )


async def build_bundle(
    session: AsyncSession, *, workspace_id: uuid.UUID
) -> Artifact:
    """Everything at once, with a README that says what each file is.

    A folder of eight CSVs with no explanation is only marginally better than one
    HTML blob. The manifest names each file, says how many rows it holds, and states
    the position the whole bundle describes — so the zip can be forwarded to someone
    who was never shown the screen.
    """
    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        raise ValueError("workspace not found")

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
    base = f"revenueproof-{_slug(workspace.company_name)}"

    artifacts: list[Artifact] = []
    failures: list[str] = []
    for key, label, _kind in ARTIFACTS:
        try:
            artifacts.append(
                await build_artifact(session, workspace_id=workspace_id, key=key)
            )
        except Exception as exc:  # noqa: BLE001
            # One unbuildable table must not cost the reader the other eight, but it
            # is recorded in the manifest rather than silently omitted — a bundle
            # missing a file with no explanation is a bundle nobody can trust.
            failures.append(f"{label} ({key}): could not be built — {exc}")

    manifest = _manifest(workspace, artifacts, failures, stamp)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("README.txt", manifest)
        for artifact in artifacts:
            folder = "report" if artifact.key == "report" else "data"
            archive.writestr(f"{folder}/{artifact.filename}", artifact.body)

    return Artifact(
        "bundle",
        f"{base}-full-export-{stamp}.zip",
        "application/zip",
        buffer.getvalue(),
    )


def _manifest(
    workspace: Workspace,
    artifacts: list[Artifact],
    failures: list[str],
    stamp: str,
) -> str:
    label = {key: text for key, text, _ in ARTIFACTS}
    lines = [
        "RevenueProof — full export",
        "=" * 60,
        "",
        f"Company          {workspace.company_name}",
        f"Reporting period {workspace.reporting_period_start} to "
        f"{workspace.reporting_period_end}",
        f"Currency         {workspace.base_currency}",
        f"Exported         {datetime.now(UTC).strftime('%d %B %Y at %H:%M UTC')}",
        "",
        "WHAT THIS IS",
        "-" * 60,
        "The evidence position for the reporting period above: what was claimed,",
        "what the evidence supports, and what is still open. Figures that have not",
        "been published are included and marked as such — a gap you can see is",
        "worth more than a total you cannot check.",
        "",
        "This is not investment advice and does not certify revenue.",
        "",
        "FILES",
        "-" * 60,
    ]
    for artifact in artifacts:
        rows = max(artifact.body.count(b"\n") - 1, 0)
        folder = "report/" if artifact.key == "report" else "data/"
        detail = f"{rows} rows" if artifact.key != "report" else "self-contained HTML"
        lines.append(
            f"{folder}{artifact.filename}\n"
            f"    {label.get(artifact.key, artifact.key)} — {detail}"
        )
    if failures:
        lines += ["", "NOT INCLUDED", "-" * 60, *failures]
    lines += [
        "",
        "READING THE CSVs",
        "-" * 60,
        "Every money column appears twice: `_display` is grouped and carries the",
        "currency, for reading; `_exact` is a plain decimal, for re-parsing. Neither",
        "is a float — amounts are held as integer minor units throughout and only",
        "formatted at this boundary.",
        "",
        "Every classified item carries the rule that produced it, what evidence was",
        "missing, and whether it was published. `status_for_reviewer` is the column",
        "to sort on: it separates what is settled from what is still to decide.",
    ]
    return "\n".join(lines) + "\n"


def catalogue() -> list[dict[str, str]]:
    """What is available to download, for the UI to render as buttons."""
    return [
        {"key": key, "label": label, "format": kind} for key, label, kind in ARTIFACTS
    ]


def as_json(payload: Any) -> bytes:
    return json.dumps(payload, indent=2, default=str).encode()
