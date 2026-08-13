"""Seed the Razorpay test account with payments against the live Zoho invoices.

Razorpay has no server-side "create a captured payment" call — money has to arrive
through Checkout, which is the honest constraint: their test mode simulates a real
customer paying, not a database row being written. So this creates **payment links**
over the API, and a companion browser script pays them with test cards. What lands in
the account afterwards is a genuine captured payment with a real `pay_` id, real fee
and tax fields, and a real settlement lifecycle.

Amounts come from the **live Zoho invoices**, not from the dataset, because the live
org is not GST-registered and therefore holds totals 18% below the dataset's. Seeding
from the dataset would guarantee that nothing reconciles.

    python -m scripts.seed_razorpay --plan          # what it would create
    python -m scripts.seed_razorpay --create-links  # writes .secrets/razorpay-links.json
    python -m scripts.seed_razorpay --refunds       # after the links are paid

The subset is deliberate. Every §19 adversarial case that Razorpay can express is
included; the bulk of routine invoices is not, because each payment costs a browser
round trip and a hundred identical successes prove nothing the first ten do not.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx

from app.connectors.auth import access_token_for
from app.core.config import settings
from app.models.enums import SourceSystem

LINKS_FILE = Path("/Users/mayankbadalia/revenueproof/.secrets/razorpay-links.json")
RAZORPAY = "https://api.razorpay.com/v1"
#: Undocumented test-mode ceiling for a single payment link, found by hitting it.
MAX_LINK_PAISE = 500_000_00
ZOHO = "https://www.zohoapis.in/books/v3"

#: Invoice numbers to pay, and why each one is here.
PLAN: list[tuple[str, str]] = [
    ("INV-2026-001", "large one-time — the ARR inflation case"),
    ("INV-2026-002", "routine paid subscription"),
    ("INV-2026-101", "Ironbridge instalment 1 of 3 against one invoice"),
    ("INV-2026-102", "Ironbridge instalment 2 of 3"),
    ("INV-2026-103", "Ironbridge instalment 3 of 3"),
    ("INV-2026-034", "Terrace — partial settlement, leaves a balance"),
    ("INV-2026-051", "Quantum — later partially refunded"),
    ("INV-2026-080", "Halcyon — later charged back"),
    ("INV-2026-090", "paid then fully refunded — the complete-looking evidence case"),
]

#: Refunds to raise after the payments exist: invoice number → fraction of the payment.
REFUND_PLAN: dict[str, float] = {
    "INV-2026-090": 1.0,    # full refund — must classify REFUNDED_OR_REVERSED
    "INV-2026-080": 1.0,    # chargeback stand-in; Razorpay disputes cannot be forced in test
    "INV-2026-051": 0.25,   # partial — must reduce, not erase, verified revenue
}


def _auth() -> tuple[str, str]:
    if not (settings.razorpay_key_id and settings.razorpay_key_secret):
        raise SystemExit("Razorpay keys are not configured")
    return settings.razorpay_key_id, settings.razorpay_key_secret


async def live_invoices() -> dict[str, dict[str, Any]]:
    """The invoices as Zoho actually holds them, keyed by invoice number."""
    token = await access_token_for(SourceSystem.ZOHO_BOOKS)
    if not token:
        raise SystemExit("Zoho credential missing — cannot read live invoice amounts")

    found: dict[str, dict[str, Any]] = {}
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    params = {"organization_id": settings.zoho_organization_id, "per_page": 200}
    async with httpx.AsyncClient(timeout=60, headers=headers) as client:
        page = 1
        while True:
            response = await client.get(f"{ZOHO}/invoices", params={**params, "page": page})
            response.raise_for_status()
            body = response.json()
            for invoice in body.get("invoices", []):
                found[invoice["invoice_number"]] = invoice
            if not body.get("page_context", {}).get("has_more_page"):
                break
            page += 1
    return found


async def create_links(dry_run: bool) -> None:
    invoices = await live_invoices()
    existing = json.loads(LINKS_FILE.read_text()) if LINKS_FILE.is_file() else {}
    created = dict(existing)

    async with httpx.AsyncClient(timeout=60, auth=_auth()) as client:
        for number, reason in PLAN:
            invoice = invoices.get(number)
            if invoice is None:
                print(f"  skip {number}: not present in Zoho")
                continue
            if number in created:
                print(f"  skip {number}: link already created")
                continue

            paise = int(round(float(invoice["total"]) * 100))
            customer = invoice.get("customer_name") or "Customer"
            print(f"  {number}: ₹{paise/100:,.2f} — {reason}")
            if dry_run:
                continue

            if paise > MAX_LINK_PAISE:
                # Razorpay test mode caps a single payment link. ₹5,00,000 is
                # accepted, ₹6,00,000 is rejected outright — an undocumented ceiling
                # found by hitting it. Splitting would misrepresent the case, so the
                # invoice is reported as unpayable rather than quietly altered.
                print(f"    skipped: ₹{paise/100:,.2f} exceeds the test-mode link cap")
                continue

            payload = {
                    "amount": paise,
                    "currency": "INR",
                    # The invoice number must survive into the payment record: Feature 4
                    # scores invoice↔payment candidates partly on reference agreement.
                    "description": f"{number} payment for {customer}",
                    "customer": {
                        "name": customer[:50],
                        "email": "founder@revenueproof.test",
                        "contact": "+919000000000",
                    },
                    "notify": {"sms": False, "email": False},
                    "reminder_enable": False,
                    "notes": {"invoice_number": number, "customer_name": customer},
            }

            # Razorpay rate-limits link creation well below any documented figure —
            # five rapid calls trigger it. Pace, and back off when it still lands.
            for attempt in range(4):
                response = await client.post(f"{RAZORPAY}/payment_links", json=payload)
                if response.status_code != 429:
                    break
                wait = 8 * (attempt + 1)
                print(f"    rate-limited, waiting {wait}s")
                await asyncio.sleep(wait)

            if response.status_code >= 300:
                print(f"    failed: {response.status_code} {response.text[:200]}")
                continue
            body = response.json()
            await asyncio.sleep(3)
            created[number] = {
                "id": body["id"],
                "short_url": body["short_url"],
                "amount": paise,
                "customer": customer,
                "reason": reason,
                "paid": False,
            }

    if not dry_run:
        LINKS_FILE.write_text(json.dumps(created, indent=2))
        print(f"\nwrote {len(created)} links to {LINKS_FILE}")


async def raise_refunds() -> None:
    """Refund captured payments, so the dataset's reversal cases exist for real."""
    links = json.loads(LINKS_FILE.read_text()) if LINKS_FILE.is_file() else {}
    async with httpx.AsyncClient(timeout=60, auth=_auth()) as client:
        payments = (await client.get(f"{RAZORPAY}/payments", params={"count": 100})).json()
        # The invoice number lives in `notes`, not `description` — a payment link
        # overwrites `description` with its own receipt id.
        by_link = {
            (p.get("notes") or {}).get("invoice_number"): p
            for p in payments.get("items", [])
            if p.get("status") == "captured"
        }

        for number, fraction in REFUND_PLAN.items():
            payment = by_link.get(number)
            if payment is None:
                print(f"  {number}: no captured payment yet — pay its link first")
                continue
            if int(payment.get("amount_refunded") or 0) > 0:
                print(f"  {number}: already refunded")
                continue
            amount = int(round(int(payment["amount"]) * fraction))
            response = await client.post(
                f"{RAZORPAY}/payments/{payment['id']}/refund",
                json={"amount": amount, "notes": {"invoice_number": number}},
            )
            if response.status_code >= 300:
                print(f"  {number}: refund failed {response.status_code} {response.text[:160]}")
                continue
            print(f"  {number}: refunded ₹{amount/100:,.2f} -> {response.json()['id']}")
    _ = links


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--create-links", action="store_true")
    parser.add_argument("--refunds", action="store_true")
    args = parser.parse_args()

    if args.plan or args.create_links:
        await create_links(dry_run=args.plan)
    if args.refunds:
        await raise_refunds()
    if not (args.plan or args.create_links or args.refunds):
        parser.error("choose --plan, --create-links or --refunds")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
