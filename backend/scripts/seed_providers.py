"""Seed the connected provider sandboxes with the §15 demonstration dataset.

This is the step that turns "the connectors are written against the documented API
shapes" into "the connectors have met the real API". The same 20 customers, 55
invoices and their payments are pushed into a real Zoho Books organisation and a real
HubSpot account, then read back out through the normal ingestion path — so any
difference between the documented response shape and the actual one shows up as a
parsing failure rather than as a silent wrong number months later.

**What this creates is still invented data.** What becomes real is the integration:
genuine HTTP, genuine provider IDs, genuine response shapes, genuine error
behaviour. The workspace that ingests it is labelled `sandbox`, never `production`.

    python -m scripts.seed_providers --zoho --hubspot [--limit N] [--dry-run]

Idempotent by natural key: a contact with the same name, or an invoice with the same
number, is reused rather than duplicated. Re-running after a partial failure resumes
instead of doubling the org.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

import httpx

from app.connectors.auth import access_token_for
from app.connectors.synthetic import transactions as synthetic
from app.core.config import settings
from app.models.enums import SourceSystem

ZOHO_API = {
    "in": "https://www.zohoapis.in",
    "com": "https://www.zohoapis.com",
    "eu": "https://www.zohoapis.eu",
    "au": "https://www.zohoapis.com.au",
}
HUBSPOT_API = "https://api.hubapi.com"


def log(message: str) -> None:
    print(message, flush=True)


# ---------------------------------------------------------------------------
# Zoho Books
# ---------------------------------------------------------------------------


async def seed_zoho(limit: int | None, dry_run: bool) -> dict[str, Any]:
    token = await access_token_for(SourceSystem.ZOHO_BOOKS)
    if not token:
        return {"skipped": "no Zoho credential"}

    base = f"{ZOHO_API.get(settings.zoho_region, ZOHO_API['in'])}/books/v3"
    org = settings.zoho_organization_id
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    params = {"organization_id": org}
    summary = {"contacts_created": 0, "contacts_existing": 0,
               "invoices_created": 0, "invoices_existing": 0, "errors": []}

    async with httpx.AsyncClient(timeout=60, headers=headers) as client:
        # GST fields are rejected outright ("Invalid Element gst_no") unless the
        # organisation is registered for GST. Worth detecting rather than retrying:
        # GSTIN is a strong identity signal — two GSTINs sharing a PAN are one legal
        # entity — so its absence weakens Feature 2 on live data and should be
        # reported, not silently worked around.
        org_response = await client.get(f"{base}/organizations", params=params)
        org_response.raise_for_status()
        orgs = org_response.json().get("organizations", [])
        gst_enabled = bool(orgs and orgs[0].get("is_gst_registered"))
        summary["gst_enabled"] = gst_enabled
        if not gst_enabled:
            log("  zoho: organisation is not GST-registered — seeding without GSTINs")

        # --- contacts ---------------------------------------------------
        existing = await client.get(f"{base}/contacts", params={**params, "per_page": 200})
        existing.raise_for_status()
        by_name = {
            c["contact_name"]: c["contact_id"]
            for c in existing.json().get("contacts", [])
        }
        log(f"  zoho: {len(by_name)} contacts already present")

        contacts = synthetic.zoho_contacts()[: limit or None]
        id_map: dict[str, str] = {}
        for contact in contacts:
            name = contact["contact_name"]
            if name in by_name:
                id_map[contact["contact_id"]] = by_name[name]
                summary["contacts_existing"] += 1
                continue
            if dry_run:
                log(f"  zoho: would create contact {name}")
                continue

            body = {
                "contact_name": name,
                "company_name": contact["company_name"],
                "currency_code": "INR",
                "contact_type": "customer",
            }
            # Zoho rejects the whole payload on one malformed optional field, so
            # optional identifiers are attached only when actually present.
            if contact.get("email"):
                body["contact_persons"] = [
                    {
                        "first_name": name.split()[0][:40],
                        "email": contact["email"],
                        "is_primary_contact": True,
                    }
                ]
            if contact.get("website"):
                body["website"] = contact["website"]
            if gst_enabled and contact.get("gst_no"):
                body["gst_no"] = contact["gst_no"]
                body["gst_treatment"] = "business_gst"

            response = await client.post(f"{base}/contacts", params=params, json=body)
            payload = response.json() if response.content else {}
            if response.status_code >= 300 or payload.get("code"):
                summary["errors"].append(f"contact {name}: {payload.get('message', response.status_code)}")
                continue
            id_map[contact["contact_id"]] = payload["contact"]["contact_id"]
            summary["contacts_created"] += 1
            log(f"  zoho: contact {name} -> {payload['contact']['contact_id']}")

        # --- invoices ---------------------------------------------------
        existing_inv = await client.get(f"{base}/invoices", params={**params, "per_page": 200})
        existing_inv.raise_for_status()
        numbers = {i["invoice_number"] for i in existing_inv.json().get("invoices", [])}
        log(f"  zoho: {len(numbers)} invoices already present")

        for invoice in synthetic.zoho_invoices()[: limit or None]:
            number = invoice["invoice_number"]
            if number in numbers:
                summary["invoices_existing"] += 1
                continue
            customer_id = id_map.get(invoice["customer_id"])
            if not customer_id:
                continue  # its customer was outside the --limit slice
            if dry_run:
                log(f"  zoho: would create invoice {number}")
                continue

            line = invoice["line_items"][0]
            body = {
                "customer_id": customer_id,
                "invoice_number": number,
                "date": invoice["date"],
                "due_date": invoice["due_date"],
                "reference_number": invoice.get("reference_number", ""),
                "line_items": [
                    {
                        "name": line["name"][:100],
                        "description": line["description"],
                        "rate": line["rate"],
                        "quantity": line["quantity"],
                    }
                ],
            }
            # Zoho auto-generates invoice numbers and refuses a supplied one unless
            # this flag is set. Keeping the dataset's own numbers matters: Feature 4
            # matches payments to invoices partly by reference, and the §19 cases
            # (one bank credit settling four invoices, three instalments against one)
            # are defined in terms of those exact numbers.
            response = await client.post(
                f"{base}/invoices",
                params={**params, "ignore_auto_number_generation": "true"},
                json=body,
            )
            payload = response.json() if response.content else {}
            if response.status_code >= 300 or payload.get("code"):
                summary["errors"].append(
                    f"invoice {number}: {payload.get('message', response.status_code)}"
                )
                continue
            summary["invoices_created"] += 1
            log(f"  zoho: invoice {number} -> {payload['invoice']['invoice_id']}")

    return summary


# ---------------------------------------------------------------------------
# HubSpot
# ---------------------------------------------------------------------------


async def seed_zoho_credit_notes(dry_run: bool) -> dict[str, Any]:
    """Mirror the *real* Razorpay refunds as accounting-side credit notes.

    Deliberately driven from what Razorpay actually refunded rather than from the
    dataset: the point of the live run is that the accounting system and the
    processor tell the same story about the same money. Copying the dataset's
    reversals here would put credit notes against invoices no one ever refunded.
    """
    token = await access_token_for(SourceSystem.ZOHO_BOOKS)
    if not token:
        return {"skipped": "no Zoho credential"}

    base = f"{ZOHO_API.get(settings.zoho_region, ZOHO_API['in'])}/books/v3"
    params = {"organization_id": settings.zoho_organization_id}
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    summary = {"created": 0, "existing": 0, "errors": []}

    async with httpx.AsyncClient(timeout=60, headers=headers) as client:
        # `httpx.get` is blocking: inside an async function it stalls the event
        # loop until the response arrives. The client is already open — use it.
        async with httpx.AsyncClient(timeout=60) as razorpay:
            response = await razorpay.get(
                "https://api.razorpay.com/v1/refunds",
                auth=(settings.razorpay_key_id, settings.razorpay_key_secret),
                params={"count": 100},
            )
        refunds = response.json().get("items", [])

        invoices_response = await client.get(f"{base}/invoices", params={**params, "per_page": 200})
        invoices_response.raise_for_status()
        invoices = {i["invoice_number"]: i for i in invoices_response.json().get("invoices", [])}

        existing = await client.get(f"{base}/creditnotes", params={**params, "per_page": 200})
        existing.raise_for_status()
        present = {c.get("reference_number") for c in existing.json().get("creditnotes", [])}
        log(f"  zoho: {len(present)} credit notes already present")

        for refund in refunds:
            number = (refund.get("notes") or {}).get("invoice_number")
            invoice = invoices.get(number)
            if not invoice:
                continue
            if number in present:
                summary["existing"] += 1
                continue

            amount = int(refund["amount"]) / 100
            log(f"  zoho: credit note for {number} ₹{amount:,.2f}")
            if dry_run:
                continue

            response = await client.post(
                f"{base}/creditnotes",
                params={**params, "ignore_auto_number_generation": "true"},
                json={
                    "customer_id": invoice["customer_id"],
                    "creditnote_number": f"CN-{number.split('-')[-1]}",
                    "reference_number": number,
                    "date": str(invoice["date"]),
                    "line_items": [
                        {
                            "name": f"Refund against {number}",
                            "description": f"Mirrors Razorpay refund {refund['id']}",
                            "rate": amount,
                            "quantity": 1,
                        }
                    ],
                },
            )
            payload = response.json() if response.content else {}
            if response.status_code >= 300 or payload.get("code"):
                summary["errors"].append(
                    f"{number}: {payload.get('message', response.status_code)}"
                )
                continue
            summary["created"] += 1

    return summary


async def seed_hubspot(limit: int | None, dry_run: bool) -> dict[str, Any]:
    token = await access_token_for(SourceSystem.HUBSPOT)
    if not token:
        return {"skipped": "no HubSpot credential"}

    headers = {"Authorization": f"Bearer {token}", "content-type": "application/json"}
    summary = {"created": 0, "existing": 0, "errors": []}

    async with httpx.AsyncClient(timeout=60, headers=headers) as client:
        listing = await client.get(
            f"{HUBSPOT_API}/crm/v3/objects/companies",
            params={"limit": 100, "properties": "name,domain"},
        )
        listing.raise_for_status()
        present = {
            (c["properties"].get("name") or "").strip()
            for c in listing.json().get("results", [])
        }
        log(f"  hubspot: {len(present)} companies already present")

        for company in synthetic.hubspot_companies()[: limit or None]:
            props = company["properties"]
            if props["name"] in present:
                summary["existing"] += 1
                continue
            if dry_run:
                log(f"  hubspot: would create {props['name']}")
                continue

            response = await client.post(
                f"{HUBSPOT_API}/crm/v3/objects/companies",
                json={
                    "properties": {
                        "name": props["name"],
                        "domain": props.get("domain") or "",
                        "city": props.get("city") or "",
                        "country": props.get("country") or "",
                        "lifecyclestage": props.get("lifecyclestage") or "customer",
                    }
                },
            )
            if response.status_code >= 300:
                summary["errors"].append(f"{props['name']}: {response.text[:160]}")
                continue
            summary["created"] += 1
            log(f"  hubspot: {props['name']} -> {response.json()['id']}")

    return summary


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zoho", action="store_true")
    parser.add_argument("--hubspot", action="store_true")
    parser.add_argument("--credit-notes", action="store_true")
    parser.add_argument("--limit", type=int, default=None, help="seed only the first N of each")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not (args.zoho or args.hubspot or args.credit_notes):
        parser.error("choose at least one of --zoho / --hubspot / --credit-notes")

    if args.zoho:
        log("Zoho Books:")
        log(f"  {await seed_zoho(args.limit, args.dry_run)}")
    if args.credit_notes:
        log("Zoho credit notes (mirroring real Razorpay refunds):")
        log(f"  {await seed_zoho_credit_notes(args.dry_run)}")
    if args.hubspot:
        log("HubSpot:")
        log(f"  {await seed_hubspot(args.limit, args.dry_run)}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
