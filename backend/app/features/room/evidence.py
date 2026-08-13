"""Evidence-path tracing — Feature 8, sub-features 1 and 2.

The product's whole claim is that **every supported rupee is traceable**. This is
where that claim becomes checkable: for any recognised amount, the chain

    Customer → Contract → Invoice → Payment → Bank Receipt → Refund

is reconstructed from the records that actually produced it, with the rule, the
critic's verdict and the evidence ids attached at each step.

Two decisions worth stating:

**Only approved records enter the graph.** Feature 7 publishes an item once it has
survived both the arithmetic and the critic; anything else is still a proposal, and
a diligence room that presents proposals as evidence is worse than one that presents
nothing. Unpublished items are still *listed* — with the reason they are not
published — because the gap is exactly what a reviewer is looking for.

**The chain is built in Postgres, mirrored to Neo4j.** The relational rows are the
system of record and the trace has to be exactly reproducible; Neo4j is for the
questions that are about *paths* — who else touches this bank account, is there a
route between these two customers. Building the trace from the graph would make an
auditable figure depend on a projection that can drift.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.money import from_minor_units
from app.models import (
    Allocation,
    BankTransaction,
    Citation,
    Contract,
    CriticDecision,
    CustomerEntity,
    Invoice,
    Payment,
    Refund,
    RevenueItem,
)
from app.models.enums import RevenueClass

#: The order the chain is presented in. A reader should be able to follow it top to
#: bottom and see the money arrive.
CHAIN = ("customer", "contract", "invoice", "payment", "bank", "refund")


def _money(minor: int, currency: str) -> dict[str, Any]:
    return {
        "minor": minor,
        "currency": currency,
        "display": f"{currency} {from_minor_units(minor, currency):,.2f}",
    }


@dataclass
class EvidenceNode:
    kind: str
    id: str
    label: str
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "id": self.id, "label": self.label, **self.detail}


@dataclass
class EvidenceTrace:
    """One recognised amount and everything behind it."""

    item_id: str
    description: str
    classification: str
    is_published: bool
    recognized: dict[str, Any] = field(default_factory=dict)
    gross: dict[str, Any] = field(default_factory=dict)
    rule_id: str = ""
    rule_explanation: str = ""
    missing_evidence: list[str] = field(default_factory=list)
    nodes: list[EvidenceNode] = field(default_factory=list)
    edges: list[dict[str, str]] = field(default_factory=list)
    critic: dict[str, Any] | None = None
    #: Where the chain stops, and why. A break is the finding, not an error.
    breaks: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """A chain with no breaks. Exposed here rather than only in the payload so
        callers and tests read the same thing the API does."""
        return not self.breaks

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "description": self.description,
            "classification": self.classification,
            "is_published": self.is_published,
            "recognized": self.recognized,
            "gross": self.gross,
            "rule_id": self.rule_id,
            "rule_explanation": self.rule_explanation,
            "missing_evidence": self.missing_evidence,
            "nodes": [n.as_dict() for n in self.nodes],
            "edges": self.edges,
            "critic": self.critic,
            "breaks": self.breaks,
            "complete": self.complete,
        }


async def trace_item(
    session: AsyncSession, *, workspace_id: uuid.UUID, item_id: uuid.UUID
) -> EvidenceTrace | None:
    """Rebuild the evidence chain behind one recognised amount."""
    item = await session.get(RevenueItem, item_id)
    if item is None or item.workspace_id != workspace_id:
        return None

    currency = item.currency
    trace = EvidenceTrace(
        item_id=str(item.id),
        description=item.description,
        classification=str(item.classification),
        is_published=bool(item.is_published),
        recognized=_money(item.recognized_amount, currency),
        gross=_money(item.gross_amount, currency),
        rule_id=item.rule_id or "",
        rule_explanation=item.rule_explanation or "",
        missing_evidence=list(item.missing_evidence or []),
    )

    def link(source: EvidenceNode | None, target: EvidenceNode) -> None:
        trace.nodes.append(target)
        if source is not None:
            trace.edges.append({"source": source.id, "target": target.id})

    # --- customer ---------------------------------------------------------
    customer_node: EvidenceNode | None = None
    if item.customer_entity_id:
        customer = await session.get(CustomerEntity, item.customer_entity_id)
        if customer is not None:
            customer_node = EvidenceNode(
                "customer",
                str(customer.id),
                customer.canonical_name,
                {
                    "aliases": list(customer.known_aliases or [])[:6],
                    "domains": list(customer.domains or [])[:3],
                    "tax_identifiers": list(customer.tax_identifiers or [])[:3],
                    "related_party": customer.related_party_status,
                },
            )
            link(None, customer_node)
    if customer_node is None:
        trace.breaks.append(
            "No resolved customer: this amount is not attributed to a canonical entity."
        )

    # --- contract, with the clause it was read from ------------------------
    contract_node: EvidenceNode | None = None
    if item.contract_id:
        contract = await session.get(Contract, item.contract_id)
        if contract is not None:
            citations = (
                await session.execute(
                    select(Citation).where(Citation.contract_id == contract.id)
                )
            ).scalars().all()
            verified = [c for c in citations if c.verified]
            contract_node = EvidenceNode(
                "contract",
                str(contract.id),
                contract.document_name,
                {
                    "recurring": _money(contract.recurring_amount, contract.currency),
                    "one_time": _money(contract.one_time_amount, contract.currency),
                    "start_date": str(contract.start_date) if contract.start_date else None,
                    "end_date": str(contract.end_date) if contract.end_date else None,
                    "citations_verified": len(verified),
                    "citations_total": len(citations),
                    # The actual words the figure was read from — the deepest link
                    # in the chain, and the one a sceptical reader asks for first.
                    "quotes": [
                        {
                            "field": c.field_name,
                            "page": c.page_number,
                            "quote": c.quote[:240],
                            "verified": c.verified,
                        }
                        for c in verified[:4]
                    ],
                },
            )
            link(customer_node, contract_node)
    if contract_node is None and item.classification in {
        RevenueClass.VERIFIED_RECURRING,
        RevenueClass.CONTRACTED_UNPAID,
    }:
        trace.breaks.append("No contract behind an amount classified as contractual.")

    # --- invoice ----------------------------------------------------------
    invoice_node: EvidenceNode | None = None
    if item.invoice_id:
        invoice = await session.get(Invoice, item.invoice_id)
        if invoice is not None:
            invoice_node = EvidenceNode(
                "invoice",
                str(invoice.id),
                invoice.invoice_number or invoice.source_id,
                {
                    "total": _money(invoice.total, invoice.currency),
                    "amount_due": _money(invoice.amount_due, invoice.currency),
                    "status": str(invoice.status),
                    "issue_date": str(invoice.issue_date) if invoice.issue_date else None,
                    "source_system": str(invoice.source_system),
                },
            )
            link(contract_node or customer_node, invoice_node)

    # --- payments, and the bank credits that confirm them ------------------
    allocations = (
        await session.execute(
            select(Allocation).where(
                Allocation.workspace_id == workspace_id,
                Allocation.invoice_id == item.invoice_id,
                Allocation.reversed_at.is_(None),
            )
        )
    ).scalars().all() if item.invoice_id else []

    payment_ids = {a.payment_id for a in allocations if a.payment_id}
    if item.payment_id:
        payment_ids.add(item.payment_id)

    bank_confirmed = False
    for payment_id in sorted(payment_ids, key=str):
        payment = await session.get(Payment, payment_id)
        if payment is None:
            continue
        payment_node = EvidenceNode(
            "payment",
            str(payment.id),
            payment.reference or payment.source_id,
            {
                "amount": _money(payment.amount, payment.currency),
                "fee": _money(payment.fee, payment.currency),
                "refunded": _money(payment.amount_refunded, payment.currency),
                "status": str(payment.status),
                "captured_at": payment.payment_time.isoformat()
                if payment.payment_time
                else None,
                "source_system": str(payment.source_system),
            },
        )
        link(invoice_node or customer_node, payment_node)

        for allocation in allocations:
            if allocation.payment_id != payment_id or not allocation.bank_transaction_id:
                continue
            bank = await session.get(BankTransaction, allocation.bank_transaction_id)
            if bank is None:
                continue
            bank_confirmed = True
            link(
                payment_node,
                EvidenceNode(
                    "bank",
                    str(bank.id),
                    bank.counterparty or bank.reference or "bank credit",
                    {
                        "amount": _money(bank.amount, bank.currency),
                        "direction": str(bank.direction),
                        "transaction_date": str(bank.transaction_date),
                        "narration": (bank.narration or "")[:200],
                    },
                ),
            )

        for refund in (
            await session.execute(
                select(Refund).where(
                    Refund.workspace_id == workspace_id, Refund.payment_id == payment_id
                )
            )
        ).scalars().all():
            link(
                payment_node,
                EvidenceNode(
                    "refund",
                    str(refund.id),
                    "chargeback" if refund.is_chargeback else "refund",
                    {
                        "amount": _money(refund.amount, refund.currency),
                        "refunded_at": refund.refund_time.isoformat()
                        if refund.refund_time
                        else None,
                        "reason": (refund.reason or "")[:200],
                        "is_chargeback": refund.is_chargeback,
                    },
                ),
            )

    if RevenueClass(item.classification).counts_as_verified:
        if not payment_ids:
            trace.breaks.append("No payment evidence behind a verified amount.")
        elif not bank_confirmed:
            trace.breaks.append(
                "No independent bank credit confirms this receipt — the processor is "
                "the only source saying the money arrived."
            )

    # --- the critic's verdict ---------------------------------------------
    decision = (
        await session.execute(
            select(CriticDecision).where(
                CriticDecision.workspace_id == workspace_id,
                CriticDecision.revenue_item_id == item.id,
            )
        )
    ).scalars().first()
    if decision is not None:
        trace.critic = {
            "verdict": str(decision.verdict),
            "issue_codes": decision.issue_codes,
            "reasoning": decision.reasoning,
            "routed_to_feature": decision.routed_to_feature,
            "settled_by": (
                "deterministic checks"
                if decision.deterministic_findings
                else ("the critic model" if decision.critic_model else "no issues found")
            ),
        }
    elif item.is_published:
        trace.breaks.append(
            "Published without a recorded critic verdict — run the critic."
        )
    return trace
