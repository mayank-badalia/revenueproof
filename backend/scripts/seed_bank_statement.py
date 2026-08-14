"""Build a bank statement that matches a workspace's *own* payments.

The four connectable providers — Razorpay, Zoho Books, HubSpot, Drive — contain no
bank. So a workspace built from live accounts has a processor saying "I captured this"
and nothing independent confirming the money arrived, and every receipt stops at
MODERATE evidence. The built-in demonstration statement does not close that gap: it
carries the §15 dataset's amounts, while a live Zoho org that is not GST-registered
holds totals 18% below them, so the names line up and not one figure does. Measured on
the first live workspace: 62 bank rows loaded, **zero** payments confirmed by any of
them.

This generates the statement the live data implies instead. For every payment the
processor actually settled it writes the credit the bank would have shown — the amount
**net of processor fee and tax**, on a realistic settlement date — and for every refund
the debit that took the money back out. The result reconciles because it is derived
from the same rows the reconciler will compare it against.

    python -m scripts.seed_bank_statement --workspace <uuid>            # write the CSV
    python -m scripts.seed_bank_statement --workspace <uuid> --upload   # and ingest it

**This is generated evidence, not a bank export.** It is written for a workspace whose
providers are test accounts, so that the settlement leg of the chain can be shown at
all. It is ingested through the ordinary CSV path and is marked synthetic in the vault
like any other demonstration source, so nothing downstream can mistake it for a
statement a bank produced. Never point it at a workspace holding a real company's
books: it would assert receipts that no bank ever confirmed.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import sys
import uuid
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from app.core.db import dispose_engine, get_sessionmaker
from app.models import Payment, Refund, Workspace
from app.models.enums import PaymentStatus

#: T+2 is Razorpay's ordinary settlement lag. `candidates.py` accepts -1..+10 days.
SETTLEMENT_LAG_DAYS = 2
#: A refund leaves the account a little after it is raised.
REFUND_LAG_DAYS = 1
ACCOUNT_NUMBER = "50100234567890"
OPENING_BALANCE = Decimal(2500000)


def _rupees(minor: int) -> Decimal:
    return (Decimal(minor) / 100).quantize(Decimal("0.01"))


def _counterparty(name: str | None) -> str:
    """The payer as a bank would print it — upper case, no punctuation."""
    cleaned = (name or "UNKNOWN PAYER").upper()
    return "".join(ch for ch in cleaned if ch.isalnum() or ch.isspace()).strip()


async def build_rows(workspace_id: uuid.UUID) -> tuple[list[dict[str, str]], dict]:
    """Credits for settled payments, debits for refunds, in date order."""
    async with get_sessionmaker()() as session:
        workspace = await session.get(Workspace, workspace_id)
        if workspace is None:
            raise SystemExit(f"no workspace {workspace_id}")

        payments = list(
            (
                await session.execute(
                    select(Payment)
                    .where(Payment.workspace_id == workspace_id)
                    .order_by(Payment.payment_time, Payment.source_id)
                )
            ).scalars().all()
        )
        refunds = list(
            (
                await session.execute(
                    select(Refund)
                    .where(Refund.workspace_id == workspace_id)
                    .order_by(Refund.refund_time, Refund.source_id)
                )
            ).scalars().all()
        )

        # A payment link that was never paid is not money. Only what the processor
        # reports as successful can have reached a bank account.
        settled = [p for p in payments if PaymentStatus(p.status).is_successful]
        # The canonical refund carries the processor's payment id rather than a
        # foreign key — which is what reconciliation matches on too
        # (reconciliation/service.py). Keying on the FK left every refund saying
        # "UNKNOWN PAYER", and a bank statement that cannot name who was repaid is
        # no use to the identity resolver reading it afterwards.
        by_source_id = {p.source_id: p for p in payments}

        settled_on: dict[str, object] = {}
        events: list[tuple] = []
        for index, payment in enumerate(settled, start=1):
            if not payment.payment_time:
                continue
            net = payment.amount - (payment.fee or 0) - (payment.tax or 0)
            if net <= 0:
                continue
            value_date = payment.payment_time.date() + timedelta(days=SETTLEMENT_LAG_DAYS)
            settled_on[payment.source_id] = value_date
            events.append((
                value_date,
                "credit",
                _rupees(net),
                f"RAZORPAY SETTLEMENT {_counterparty(payment.stated_customer_name)}",
                f"RAZORPAY SETTL {4000 + index}",
            ))

        for index, refund in enumerate(refunds, start=1):
            payer = by_source_id.get(refund.source_payment_id or "")
            when = refund.refund_time or (payer.payment_time if payer else None)
            if when is None:
                continue
            leaves_on = when.date() + timedelta(days=REFUND_LAG_DAYS)
            # Money cannot leave the account before it arrived. Razorpay refunds a
            # payment settled the same week, so the raw refund timestamp can precede
            # its own settlement date — which would render a statement showing the
            # repayment above the receipt, and a running balance that dips through
            # money it had not yet been credited.
            settled_date = settled_on.get(refund.source_payment_id or "")
            if settled_date is not None and leaves_on <= settled_date:
                leaves_on = settled_date + timedelta(days=1)
            label = "CHARGEBACK DEBIT" if refund.is_chargeback else "RAZORPAY REFUND"
            events.append((
                leaves_on,
                "debit",
                _rupees(refund.amount),
                f"{label} {_counterparty(payer.stated_customer_name if payer else None)}",
                f"RAZORPAY RFND {5000 + index}",
            ))

        events.sort(key=lambda e: (e[0], e[1]))

        balance = OPENING_BALANCE
        rows: list[dict[str, str]] = []
        credited = debited = Decimal(0)
        for when, direction, amount, description, reference in events:
            if direction == "credit":
                balance += amount
                credited += amount
            else:
                balance -= amount
                debited += amount
            rows.append({
                "Date": when.strftime("%d/%m/%Y"),
                "Value Date": (when + timedelta(days=1)).strftime("%d/%m/%Y"),
                "Description": description,
                "Reference": reference,
                "Debit": f"{amount:.2f}" if direction == "debit" else "",
                "Credit": f"{amount:.2f}" if direction == "credit" else "",
                "Balance": f"{balance:.2f}",
                "Account Number": ACCOUNT_NUMBER,
            })

        stats = {
            "company": workspace.company_name,
            "period": f"{workspace.reporting_period_start} to "
                      f"{workspace.reporting_period_end}",
            "payments_total": len(payments),
            "payments_settled": len(settled),
            "refunds": len(refunds),
            "credited": credited,
            "debited": debited,
            "closing": balance,
        }
        return rows, stats


def to_csv(rows: list[dict[str, str]], stats: dict) -> bytes:
    """The same shape the ordinary upload parser reads, preamble included."""
    if not rows:
        raise SystemExit(
            "no settled payments in this workspace — there is no statement to write. "
            "Pay some payment links in Razorpay Checkout first."
        )
    buffer = io.StringIO()
    buffer.write("Statement of Account\n")
    buffer.write(f"Account Holder: {stats['company']}\n")
    buffer.write(f"Period: {stats['period']}\n")
    buffer.write("\n")
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True, help="workspace UUID")
    parser.add_argument("--out", default=None, help="where to write the CSV")
    parser.add_argument("--upload", action="store_true",
                        help="ingest it into the workspace as well as writing it")
    parser.add_argument("--set-claim", action="store_true",
                        help="set the workspace's claimed revenue to the cash this "
                             "statement shows the company actually retained")
    args = parser.parse_args()

    workspace_id = uuid.UUID(args.workspace)
    rows, stats = await build_rows(workspace_id)
    content = to_csv(rows, stats)

    out = Path(args.out or f"bank_statement_{workspace_id}.csv")
    out.write_bytes(content)

    print(f"  {len(rows)} rows written to {out}")
    print(f"  {stats['payments_settled']} of {stats['payments_total']} payments settled")
    print(f"  credited INR {stats['credited']:,.2f} · debited INR {stats['debited']:,.2f}")
    print(f"  closing balance INR {stats['closing']:,.2f}")

    if args.upload:
        from app.services import ingestion

        async with get_sessionmaker()() as session:
            workspace = await session.get(Workspace, workspace_id)
            result = await ingestion.ingest_bank_csv(
                session,
                workspace_id=workspace_id,
                content=content,
                filename=out.name,
                currency=workspace.base_currency,
                # Generated from this workspace's own payments, not a bank export.
                is_synthetic=True,
            )
            await session.commit()
        print(f"  ingested: {result.canonical_written} transactions, "
              f"{len(result.errors)} errors")
        for error in result.errors[:5]:
            print(f"    {error}")

    # A claim of INR 1.5 crore against accounts holding a few lakh reads as 1.5%
    # proven however complete the evidence is, and the percentage then says more
    # about the seeding than about the company. The defensible claim for a workspace
    # built from these accounts is what the processor says it kept: settlements in,
    # refunds and chargebacks back out. The gap that remains after that is a real
    # finding — unexplained receipts, invoices never paid — rather than an artefact.
    retained = stats["credited"] - stats["debited"]
    print(f"\n  cash retained per this statement: INR {retained:,.2f}")
    if args.set_claim:
        async with get_sessionmaker()() as session:
            workspace = await session.get(Workspace, workspace_id)
            before = workspace.claimed_revenue
            workspace.claimed_revenue = int(retained * 100)
            await session.commit()
        print(f"  claimed revenue {before / 100:,.2f} → {retained:,.2f}")
    else:
        print("  (pass --set-claim to make that the workspace's claimed revenue)")

    await dispose_engine()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
