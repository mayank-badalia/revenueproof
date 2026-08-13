"""Seeded roster generation — a different company book on every run.

The §15 template proves the pipeline handles *the cases*. It cannot prove the
pipeline handles anything other than Northstar, and that is a fair thing to doubt:
a system tuned to one fixture passes its own tests and fails on first contact with
a real customer list. So this builds a fresh roster from a seed — new legal names,
new spellings per system, new domains, new GSTINs, new cities — while keeping the
*case structure* identical.

Keys are deliberately preserved. `northstar` stays the key of the largest customer
with four spellings; only its identity changes. That is what lets the invoice,
contract and bank generators stay completely ignorant of this module, and what keeps
the dataset's ground-truth totals valid: the amounts are unchanged, so every
assertion about ₹5,31,000 outstanding still means something. What changes is
everything a detector could have been accidentally tuned to.

Each case class is reproduced by construction rather than by luck:

* four spellings of one name, so entity resolution has work to do
* two genuinely different companies whose names differ by one letter, so the
  false-merge protection is exercised
* a parent and a subsidiary sharing a domain
* a related party on the founder's own domain
* an agent settling for two unrelated customers

A seed produces the same roster every time, because a demo that shows different
numbers on the second run is not a demo anyone can trust.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import replace

from .customers import SyntheticCustomer, template

#: Name fragments. Deliberately unlike the template's, and unlike each other, so a
#: generated run cannot be mistaken for the §15 one at a glance.
_FIRST = (
    "Alder", "Bramble", "Cinder", "Dunmore", "Everest", "Fernwood", "Granite",
    "Hollow", "Ironwood", "Juniper", "Kestrel", "Larkspur", "Marrow", "Nimbus",
    "Orchard", "Pinnacle", "Quarry", "Rowan", "Sable", "Thistle", "Umber",
    "Vantage", "Willow", "Xenon", "Yarrow", "Zephyr", "Cobble", "Drift",
)
_SECOND = (
    "Analytics", "Systems", "Labs", "Networks", "Dynamics", "Logistics", "Health",
    "Retail", "Education", "Manufacturing", "Hospitality", "Software", "Ventures",
    "Media", "Industries", "Technologies", "Solutions", "Partners", "Works",
)
_SUFFIX = ("Private Limited", "LLP", "Pvt Ltd", "India Private Limited")
_CITIES = (
    ("Pune", "411004"), ("Bengaluru", "560025"), ("Mumbai", "400058"),
    ("Hyderabad", "500081"), ("Chennai", "600032"), ("Gurugram", "122002"),
    ("Ahmedabad", "380015"), ("Kolkata", "700091"), ("Jaipur", "302017"),
    ("Kochi", "682030"),
)
_STREETS = (
    "Prabhat Road", "Residency Road", "SV Road", "Banjara Hills", "Anna Salai",
    "Golf Course Road", "CG Road", "Salt Lake Sector V", "Tonk Road", "MG Road",
)
#: GSTIN state codes, so a generated tax id is at least structurally plausible.
_STATE_CODES = ("27", "29", "36", "33", "06", "24", "19", "08", "32")


def _gstin(rng: random.Random, token: str) -> str:
    """A structurally valid-looking GSTIN. Never a real one.

    The PAN in the middle is what Feature 2 extracts to recognise that two
    registrations in different states are one legal entity, so it has to be shaped
    correctly even though it is invented.
    """
    state = rng.choice(_STATE_CODES)
    letters = "".join(rng.choice("ABCDEFGHIJKLMNPQRSTUVWXYZ") for _ in range(3))
    pan = f"{letters}{token[0].upper()}{rng.choice('ABCDEFGHJKLMNPQRSTUVWXYZ')}"
    digits = f"{rng.randint(1000, 9999)}"
    check = rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ")
    return f"{state}{pan}{digits}{check}1Z{rng.randint(1, 9)}"


def _slug(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


class _NameFactory:
    """Unique two-word company names from one seeded stream."""

    def __init__(self, rng: random.Random) -> None:
        self._rng = rng
        self._used: set[str] = set()

    def make(self) -> tuple[str, str]:
        for _ in range(500):
            first = self._rng.choice(_FIRST)
            second = self._rng.choice(_SECOND)
            if (first, second) not in self._used:
                self._used.add((first, second))
                return first, second
        raise RuntimeError("name space exhausted")  # pragma: no cover

    def reserve(self, first: str, second: str) -> None:
        self._used.add((first, second))


def generate_roster(seed: int | str) -> list[SyntheticCustomer]:
    """A complete roster with the §15 case structure and none of its identities."""
    if isinstance(seed, str):
        seed = int(hashlib.sha256(seed.encode()).hexdigest()[:12], 16)
    rng = random.Random(seed)
    names = _NameFactory(rng)
    generated: list[SyntheticCustomer] = []

    # The template supplies the *shape*: which key plays which adversarial role,
    # which flags are set, which notes explain the case. Only identity is replaced,
    # so a case cannot be lost by generation.
    base = template()
    by_key = {c.key: c for c in base}

    # --- the pairs that must be related to each other ----------------------
    # A parent and its subsidiary share a domain; the near-duplicate pair differs by
    # a single letter; the related party sits on the founder's own domain. Those
    # three relationships are constructed first so the rest cannot collide with them.
    parent_first, parent_second = names.make()
    parent_domain = f"{_slug(parent_first)}group.in"

    dup_first, dup_second = names.make()
    # "Harbor"/"Harbour" in the template. Here: an inserted letter, which is the
    # same class of trap — high string similarity, different legal entity.
    dup_variant_first = dup_first[:-1] + "e" + dup_first[-1]
    names.reserve(dup_variant_first, dup_second)
    dup_second_alt = rng.choice([s for s in _SECOND if s != dup_second])

    founder_first, founder_second = names.make()
    founder_domain = f"{_slug(founder_first)}.io"

    agent_first, agent_second = names.make()
    agent_narration = f"{agent_first.upper()} {agent_second.upper()}"

    def build(
        key: str,
        first: str,
        second: str,
        *,
        domain: str | None = None,
        narration: str | None = None,
        suffix: str | None = None,
    ) -> SyntheticCustomer:
        original = by_key[key]
        legal_suffix = suffix or rng.choice(_SUFFIX)
        legal = f"{first} {second} {legal_suffix}"
        host = domain if domain is not None else f"{_slug(first)}{_slug(second)[:4]}.com"
        city, pin = rng.choice(_CITIES)
        street = rng.choice(_STREETS)
        return replace(
            original,
            legal_name=legal,
            # Accounting drops the suffix; the CRM often keeps only the domain or a
            # short form; the bank truncates and upper-cases. Four systems, four
            # spellings — which is the problem Feature 2 exists to solve.
            zoho_name=f"{first} {second}",
            crm_name=host if original.crm_name and "." in original.crm_name else first,
            bank_narration_name=(narration or f"{first.upper()} {second.upper()[:6]}"),
            domain=host if original.domain else None,
            email=f"accounts@{host}" if original.email else None,
            gstin=_gstin(rng, first) if original.gstin else None,
            address=f"{rng.randint(2, 240)} {street}, {city} {pin}",
        )

    for customer in base:
        key = customer.key
        if key == "meridian_holdings":
            generated.append(build(key, parent_first, "Holdings", domain=parent_domain))
        elif key == "meridian_systems":
            generated.append(build(key, parent_first, parent_second, domain=parent_domain))
        elif key == "blue_harbor":
            generated.append(build(key, dup_first, dup_second))
        elif key == "blue_harbour_logistics":
            # One letter apart from the entry above, different GSTIN and domain: the
            # merge that must be refused.
            generated.append(
                build(
                    key, dup_variant_first, dup_second_alt,
                    domain=f"{_slug(dup_variant_first)}-{_slug(dup_second_alt)}.com",
                )
            )
        elif key == "northstar":
            generated.append(
                build(key, founder_first, founder_second, domain=founder_domain)
            )
        elif key == "apex_holdings":
            # The related party: on the founder's own domain, which is the tell.
            generated.append(
                build(key, f"{founder_first} Founder", "Holdings", domain=founder_domain)
            )
        elif key in {"crestview", "pinnacle_foods"}:
            first, second = names.make()
            generated.append(build(key, first, second, narration=agent_narration))
        else:
            first, second = names.make()
            generated.append(build(key, first, second))

    return generated


def describe(customers: list[SyntheticCustomer]) -> dict[str, object]:
    """What was planted, for a UI that has to say what the demo contains."""
    return {
        "customers": len(customers),
        "largest_customer": next(
            (c.zoho_name for c in customers if "largest_customer" in c.tags), None
        ),
        "related_parties": [c.zoho_name for c in customers if c.related_party],
        "near_duplicate_names": [
            c.zoho_name for c in customers if "near_duplicate_name" in c.tags
        ],
        "shared_payment_account": [
            c.zoho_name for c in customers if "shared_payment_account" in c.tags
        ],
        "cases": sorted({tag for c in customers for tag in c.tags}),
    }
