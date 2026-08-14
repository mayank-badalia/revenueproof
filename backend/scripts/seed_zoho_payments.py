"""Record customer payments in Zoho Books against the live invoices.

The connected accounts proved almost nothing, and the reason was not the app. The Zoho
organisation held 55 invoices worth ~1.54 crore with **not one of them paid** — every
invoice `sent` or `overdue`, and zero customer-payment records — while the only cash
anywhere was eight Razorpay payments totalling ~11.5 lakh. Measured: 7.5% of invoiced
value collected, against 93.5% in the demonstration dataset. A verification run over
that is correct and useless; it is measuring an account nobody ever paid into.

`seed_providers.py` created the invoices and never the payments, and the ingestion side
was always ready for them — `providers.py` fetches `customerpayments` and has since it
was written. This closes that gap.

Unlike Razorpay, where money must arrive through Checkout in a browser, Zoho Books
records a customer payment over its API, so this needs no browser and no card.

    python -m scripts.seed_zoho_payments --plan     # what it would record
    python -m scripts.seed_zoho_payments --apply    # record it

It mirrors the dataset's structure rather than paying everything: the largest few
invoices are deliberately left outstanding, because "invoiced but unpaid" is one of the
states the product exists to separate, and an account where every invoice is settled
cannot demonstrate it. Invoices already covered by a real Razorpay payment are skipped
so one invoice never carries two records of the same cash.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import httpx

from app.connectors.auth import access_token_for
from app.core.config import settings
from app.models.enums import SourceSystem

ZOHO_API = {
    "in": "https://www.zohoapis.in",
    "com": "https://www.zohoapis.com",
    "eu": "https://www.zohoapis.eu",
    "au": "https://www.zohoapis.com.au",
}
RAZORPAY = "https://api.razorpay.com/v1"

#: Share of invoiced value to leave outstanding, matching the dataset's 93.5% collected.
#: Taken from the largest invoices, so the gap is material rather than rounding.
TARGET_UNPAID_SHARE = Decimal("0.065")

#: Payment lands a few days after the invoice, the way a real transfer does.
SETTLEMENT_LAG_DAYS = 4


def log(message: str) -> None:
    print(message, flush=True)


async def _zoho(client: httpx.AsyncClient, base: str, params: dict, path: str) -> list[dict]:
    """Every page of a Zoho collection."""
    out: list[dict] = []
    page = 1
    while page <= 20:
        response = await client.get(
            f"{base}/{path}", params={**params, "per_page": 200, "page": page}
        )
        response.raise_for_status()
        body = response.json()
        rows = body.get(path, [])
        out.extend(rows)
        if not body.get("page_context", {}).get("has_more_page"):
            break
        page += 1
    return out


async def razorpay_covered() -> set[str]:
    """Invoice numbers that already have real processor cash behind them."""
    if not (settings.razorpay_key_id and settings.razorpay_key_secret):
        return set()
    covered: set[str] = set()
    auth = (settings.razorpay_key_id, settings.razorpay_key_secret)
    async with httpx.AsyncClient(timeout=60, auth=auth) as client:
        response = await client.get(f"{RAZORPAY}/payments", params={"count": 100})
        if response.status_code != 200:
            return set()
        for payment in response.json().get("items", []):
            if payment.get("status") not in {"captured", "refunded", "partially_refunded"}:
                continue
            notes = payment.get("notes") or {}
            for value in list(notes.values()) + [payment.get("description")]:
                if isinstance(value, str) and value.startswith("INV-"):
                    covered.add(value)
    return covered


async def build_plan() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    token = await access_token_for(SourceSystem.ZOHO_BOOKS)
    if not token:
        raise SystemExit("No Zoho credential. Check ZOHO_REFRESH_TOKEN in .env")

    base = f"{ZOHO_API.get(settings.zoho_region, ZOHO_API['in'])}/books/v3"
    params = {"organization_id": settings.zoho_organization_id}
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}

    async with httpx.AsyncClient(timeout=90, headers=headers) as client:
        invoices = await _zoho(client, base, params, "invoices")
        existing_payments = await _zoho(client, base, params, "customerpayments")

    covered = await razorpay_covered()
    already_paid_ref = {
        str(p.get("reference_number") or "") for p in existing_payments
    }

    # Outstanding only. `balance` is what Zoho still expects for the invoice.
    open_invoices = [
        inv
        for inv in invoices
        if Decimal(str(inv.get("balance") or 0)) > 0
        and inv.get("invoice_number") not in covered
        and inv.get("invoice_number") not in already_paid_ref
    ]

    total_invoiced = sum(Decimal(str(i.get("total") or 0)) for i in invoices)
    leave_unpaid = total_invoiced * TARGET_UNPAID_SHARE

    # Largest first, so the deliberately-unpaid set is a few big invoices rather than
    # a long tail — which is what "invoiced but unpaid" looks like in a real book.
    by_size = sorted(open_invoices, key=lambda i: Decimal(str(i.get("total") or 0)), reverse=True)
    unpaid: list[dict] = []
    running = Decimal(0)
    for invoice in by_size:
        amount = Decimal(str(invoice.get("total") or 0))
        # Take the largest that still fits under the target rather than the largest
        # outright: one 32-lakh invoice against a 6.5% goal leaves a fifth of the book
        # outstanding, which is a different company from the one being demonstrated.
        if running + amount > leave_unpaid:
            continue
        unpaid.append(invoice)
        running += amount
    unpaid_ids = {i["invoice_id"] for i in unpaid}

    plan = [
        {
            "invoice_id": inv["invoice_id"],
            "invoice_number": inv.get("invoice_number"),
            "customer_id": inv.get("customer_id"),
            "customer_name": inv.get("customer_name"),
            "amount": Decimal(str(inv.get("balance") or 0)),
            "date": (
                date.fromisoformat(inv["date"]) + timedelta(days=SETTLEMENT_LAG_DAYS)
            ).isoformat()
            if inv.get("date")
            else date.today().isoformat(),
        }
        for inv in open_invoices
        if inv["invoice_id"] not in unpaid_ids
    ]

    stats = {
        "invoices_total": len(invoices),
        "invoiced_value": total_invoiced,
        "already_settled": len(invoices) - len(open_invoices),
        "covered_by_razorpay": len(covered),
        "will_pay": len(plan),
        "will_pay_value": sum(p["amount"] for p in plan),
        "left_unpaid": len(unpaid),
        "left_unpaid_value": running,
        "base": base,
        "params": params,
        "headers": headers,
    }
    return plan, stats


async def apply(plan: list[dict[str, Any]], stats: dict[str, Any]) -> int:
    recorded = 0
    errors: list[str] = []
    async with httpx.AsyncClient(timeout=90, headers=stats["headers"]) as client:
        for entry in plan:
            body = {
                "customer_id": entry["customer_id"],
                "payment_mode": "banktransfer",
                "amount": float(entry["amount"]),
                "date": entry["date"],
                "reference_number": entry["invoice_number"],
                "description": f"Settlement of {entry['invoice_number']}",
                "invoices": [
                    {
                        "invoice_id": entry["invoice_id"],
                        "amount_applied": float(entry["amount"]),
                    }
                ],
            }
            response = await client.post(
                f"{stats['base']}/customerpayments", params=stats["params"], json=body
            )
            payload = response.json() if response.content else {}
            if response.status_code >= 300 or payload.get("code"):
                errors.append(
                    f"{entry['invoice_number']}: "
                    f"{payload.get('message', response.status_code)}"
                )
                continue
            recorded += 1
            if recorded % 10 == 0:
                log(f"  recorded {recorded} of {len(plan)}…")

    log(f"\n  recorded {recorded} payments, {len(errors)} failed")
    for error in errors[:8]:
        log(f"    {error}")
    return 0 if recorded else 1


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", action="store_true", help="show what would be recorded")
    parser.add_argument("--apply", action="store_true", help="record the payments")
    args = parser.parse_args()
    if not (args.plan or args.apply):
        parser.error("choose --plan or --apply")

    plan, stats = await build_plan()

    log("Zoho Books — customer payments")
    log(f"  invoices in the org        {stats['invoices_total']}")
    log(f"  invoiced value             {stats['invoiced_value']:,.2f}")
    log(f"  already settled            {stats['already_settled']}")
    log(f"  covered by real Razorpay   {stats['covered_by_razorpay']}")
    log(f"  will record payment for    {stats['will_pay']}  "
        f"({stats['will_pay_value']:,.2f})")
    log(f"  deliberately left unpaid   {stats['left_unpaid']}  "
        f"({stats['left_unpaid_value']:,.2f})")
    if stats["invoiced_value"]:
        share = 100 * stats["will_pay_value"] / stats["invoiced_value"]
        log(f"  collected share afterwards ~{share:.1f}% "
            f"(the demonstration dataset collects 93.5%)")

    if args.plan:
        log("\n  --plan only; nothing was written.")
        return 0
    return await apply(plan, stats)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
