"""Synthetic customer roster — spec §15.

Each customer carries its name *as each system spells it*, because the naming
inconsistency is the problem Feature 2 exists to solve. A dataset where every source
agrees would make entity resolution look trivial and prove nothing.

The roster also plants the specific adversarial cases §19 requires: a related party,
two genuinely different companies with near-identical names (false-merge protection),
a parent paying for a subsidiary, and several customers sharing one payment account.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class SyntheticCustomer:
    key: str                       # internal handle used by the other generators
    legal_name: str                # as written in the contract
    zoho_name: str                 # as spelled in accounting
    crm_name: str | None           # as spelled in the CRM
    bank_narration_name: str       # as it appears in a bank statement
    domain: str | None
    email: str | None
    gstin: str | None
    address: str
    # Investigation hints the pipeline must *discover*, never read from here.
    related_party: bool = False
    related_party_note: str = ""
    # Companies that legitimately share an address or domain with another entity.
    shares_address_with: str | None = None
    pays_on_behalf_of: str | None = None
    notes: str = ""
    tags: list[str] = field(default_factory=list)


_TEMPLATE: list[SyntheticCustomer] = [
    # --- The flagship entity-resolution case (idea_features.md §2, §6.3) ---------
    SyntheticCustomer(
        key="northstar",
        legal_name="Northstar Technologies Private Limited",
        zoho_name="Northstar Tech",
        crm_name="northstar.io",
        bank_narration_name="NSTAR TECH PVT",
        domain="northstar.io",
        email="accounts@northstar.io",
        gstin="27AABCN1234F1Z5",
        address="14 Prabhat Road, Pune 411004",
        notes="Four different spellings across four systems; largest customer.",
        tags=["largest_customer", "name_variation"],
    ),
    # --- False-merge protection: two real, different companies ------------------
    SyntheticCustomer(
        key="blue_harbor",
        legal_name="Blue Harbor Analytics Private Limited",
        zoho_name="Blue Harbor Analytics",
        crm_name="Blue Harbor",
        bank_narration_name="BLUE HARBOR ANALYTICS",
        domain="blueharbor.co.in",
        email="finance@blueharbor.co.in",
        gstin="29AACCB5678K1Z2",
        address="7 Residency Road, Bengaluru 560025",
        notes="Distinct from Blue Harbour Logistics — different GSTIN and domain.",
        tags=["near_duplicate_name"],
    ),
    SyntheticCustomer(
        key="blue_harbour_logistics",
        legal_name="Blue Harbour Logistics LLP",
        zoho_name="Blue Harbour Logistics",
        crm_name="Blue Harbour",
        bank_narration_name="BLUE HARBOUR LOG LLP",
        domain="blueharbour-logistics.com",
        email="ap@blueharbour-logistics.com",
        gstin="27AAFFB9012M1Z8",
        address="221 SV Road, Mumbai 400058",
        notes="Must NOT be merged with Blue Harbor Analytics despite ~95% name similarity.",
        tags=["near_duplicate_name", "false_merge_trap"],
    ),
    # --- Parent paying for a subsidiary's contract (§18) ------------------------
    SyntheticCustomer(
        key="meridian_holdings",
        legal_name="Meridian Holdings Private Limited",
        zoho_name="Meridian Holdings",
        crm_name="Meridian Group",
        bank_narration_name="MERIDIAN HOLDINGS PVT",
        domain="meridiangroup.in",
        email="treasury@meridiangroup.in",
        gstin="07AADCM3456P1Z1",
        address="Tower B, Cyber City, Gurugram 122002",
        pays_on_behalf_of="meridian_systems",
        notes="Pays invoices raised on its subsidiary Meridian Systems.",
        tags=["parent_company"],
    ),
    SyntheticCustomer(
        key="meridian_systems",
        legal_name="Meridian Systems India Private Limited",
        zoho_name="Meridian Systems",
        crm_name="Meridian Systems",
        bank_narration_name="MERIDIAN SYSTEMS IND",
        domain="meridiangroup.in",
        email="ops@meridiangroup.in",
        gstin="07AADCM7890Q1Z4",
        address="Tower B, Cyber City, Gurugram 122002",
        shares_address_with="meridian_holdings",
        notes="Subsidiary; contract starts in a FUTURE period.",
        tags=["future_contract", "shared_address"],
    ),
    # --- Related party / founder-linked (§19 adversarial scenario) --------------
    SyntheticCustomer(
        key="apex_holdings",
        legal_name="Apex Founder Holdings Private Limited",
        zoho_name="Apex Holdings",
        crm_name=None,
        bank_narration_name="APEX FOUNDER HOLDINGS",
        domain="northstar.io",  # shares the founder's own domain — the tell
        email="rohit@northstar.io",
        gstin="27AAECA2345R1Z9",
        address="14 Prabhat Road, Pune 411004",  # same address as the company
        related_party=True,
        related_party_note=(
            "Shares a registered address and email domain with the company under "
            "review; funds move out and back within days."
        ),
        tags=["related_party", "circular_flow"],
    ),
    # --- One-time fee dressed up as recurring (§19) ------------------------------
    SyntheticCustomer(
        key="quantum_retail",
        legal_name="Quantum Retail Solutions Private Limited",
        zoho_name="Quantum Retail",
        crm_name="Quantum Retail Solutions",
        bank_narration_name="QUANTUM RETAIL SOLN",
        domain="quantumretail.in",
        email="accounts@quantumretail.in",
        gstin="33AABCQ6789S1Z3",
        address="42 Mount Road, Chennai 600002",
        notes="Large implementation fee invoiced as if it were subscription revenue.",
        tags=["one_time_as_arr"],
    ),
    # --- Ambiguous contract requiring human review (§15) ------------------------
    SyntheticCustomer(
        key="vertex_labs",
        legal_name="Vertex Labs Private Limited",
        zoho_name="Vertex Labs",
        crm_name="Vertex",
        bank_narration_name="VERTEX LABS PVT LTD",
        domain="vertexlabs.dev",
        email="billing@vertexlabs.dev",
        gstin="36AABCV1122T1Z7",
        address="Plot 9, HITEC City, Hyderabad 500081",
        notes="Contract has contradictory pricing clauses; must route to review.",
        tags=["ambiguous_contract"],
    ),
    # --- Refund / chargeback behaviour -------------------------------------------
    SyntheticCustomer(
        key="cobalt_media",
        legal_name="Cobalt Media Networks Private Limited",
        zoho_name="Cobalt Media",
        crm_name="Cobalt Media Networks",
        bank_narration_name="COBALT MEDIA NET",
        domain="cobaltmedia.tv",
        email="ap@cobaltmedia.tv",
        gstin="19AABCC3344U1Z6",
        address="16 Park Street, Kolkata 700016",
        notes="Paid then fully refunded inside 6 days — rapid-refund pattern.",
        tags=["rapid_refund"],
    ),
    SyntheticCustomer(
        key="halcyon_health",
        legal_name="Halcyon Health Technologies Private Limited",
        zoho_name="Halcyon Health",
        crm_name="Halcyon",
        bank_narration_name="HALCYON HEALTH TECH",
        domain="halcyonhealth.care",
        email="finance@halcyonhealth.care",
        gstin="24AABCH5566V1Z0",
        address="3 CG Road, Ahmedabad 380009",
        notes="Chargeback raised after the first report was generated.",
        tags=["chargeback"],
    ),
    # --- Combined / partial payment behaviour -----------------------------------
    SyntheticCustomer(
        key="silverline",
        legal_name="Silverline Education Private Limited",
        zoho_name="Silverline Education",
        crm_name="Silverline",
        bank_narration_name="SILVERLINE EDU PVT",
        domain="silverline.edu.in",
        email="accounts@silverline.edu.in",
        gstin="08AABCS7788W1Z2",
        address="D-21 Malviya Nagar, Jaipur 302017",
        notes="One bank credit settles three invoices at once.",
        tags=["combined_payment"],
    ),
    SyntheticCustomer(
        key="ironbridge",
        legal_name="Ironbridge Manufacturing Private Limited",
        zoho_name="Ironbridge Mfg",
        crm_name="Ironbridge",
        bank_narration_name="IRONBRIDGE MFG PVT",
        domain="ironbridge.co",
        email="payables@ironbridge.co",
        gstin="23AABCI9900X1Z5",
        address="Sector 3, Pithampur, Indore 454775",
        notes="Pays one invoice across three partial instalments.",
        tags=["partial_payments"],
    ),
    # --- Invoiced but never paid --------------------------------------------------
    SyntheticCustomer(
        key="tidewater",
        legal_name="Tidewater Shipping Services Private Limited",
        zoho_name="Tidewater Shipping",
        crm_name="Tidewater",
        bank_narration_name="TIDEWATER SHIPPING",
        domain="tidewatershipping.in",
        email="accounts@tidewatershipping.in",
        gstin="32AABCT1234Y1Z8",
        address="Willingdon Island, Kochi 682003",
        notes="Invoice raised and overdue; no payment evidence at all.",
        tags=["invoiced_unpaid"],
    ),
    # --- Contracted but not yet invoiced -----------------------------------------
    SyntheticCustomer(
        key="orchid_hospitality",
        legal_name="Orchid Hospitality Group Private Limited",
        zoho_name="Orchid Hospitality",
        crm_name="Orchid Group",
        bank_narration_name="ORCHID HOSPITALITY",
        domain="orchidgroup.co.in",
        email="finance@orchidgroup.co.in",
        gstin="21AABCO5678Z1Z1",
        address="Janpath, Bhubaneswar 751001",
        notes="Signed contract, nothing invoiced yet — contracted-but-unpaid.",
        tags=["contracted_unpaid"],
    ),
    # --- Payment with no supporting invoice or contract ---------------------------
    SyntheticCustomer(
        key="unknown_payer",
        legal_name="Zenith Consulting",
        zoho_name="",  # deliberately absent from accounting
        crm_name=None,
        bank_narration_name="ZENITH CONSULTING",
        domain=None,
        email=None,
        gstin=None,
        address="Unknown",
        notes="Cash arrived with no invoice and no contract behind it.",
        tags=["payment_without_support"],
    ),
    # --- Several customers paying from a single account ---------------------------
    SyntheticCustomer(
        key="crestview",
        legal_name="Crestview Retail Private Limited",
        zoho_name="Crestview Retail",
        crm_name="Crestview",
        bank_narration_name="GLOBAL PAY SERVICES",  # shared payment agent
        domain="crestview.shop",
        email="ap@crestview.shop",
        gstin="09AABCC2233A1Z4",
        address="Hazratganj, Lucknow 226001",
        notes="Pays through a shared agent account with Pinnacle Foods.",
        tags=["shared_payment_account"],
    ),
    SyntheticCustomer(
        key="pinnacle_foods",
        legal_name="Pinnacle Foods Private Limited",
        zoho_name="Pinnacle Foods",
        crm_name="Pinnacle",
        bank_narration_name="GLOBAL PAY SERVICES",  # same agent
        domain="pinnaclefoods.in",
        email="accounts@pinnaclefoods.in",
        gstin="09AABCP4455B1Z7",
        address="Gomti Nagar, Lucknow 226010",
        notes="Shares the Global Pay agent account with Crestview.",
        tags=["shared_payment_account"],
    ),
    # --- Ordinary, unremarkable customers (the control group) ---------------------
    SyntheticCustomer(
        key="lumen_software",
        legal_name="Lumen Software Private Limited",
        zoho_name="Lumen Software",
        crm_name="Lumen",
        bank_narration_name="LUMEN SOFTWARE PVT",
        domain="lumensoft.io",
        email="billing@lumensoft.io",
        gstin="27AABCL6677C1Z9",
        address="Baner, Pune 411045",
        notes="Clean monthly subscription — should verify with no findings.",
        tags=["clean"],
    ),
    SyntheticCustomer(
        key="kestrel_logistics",
        legal_name="Kestrel Logistics Private Limited",
        zoho_name="Kestrel Logistics",
        crm_name="Kestrel",
        bank_narration_name="KESTREL LOGISTICS",
        domain="kestrel-logistics.in",
        email="finance@kestrel-logistics.in",
        gstin="06AABCK8899D1Z3",
        address="Udyog Vihar, Gurugram 122016",
        notes="Clean annual subscription.",
        tags=["clean"],
    ),
    SyntheticCustomer(
        key="terrace_ventures",
        legal_name="Terrace Ventures Private Limited",
        zoho_name="Terrace Ventures",
        crm_name="Terrace",
        bank_narration_name="TERRACE VENTURES",
        domain="terrace.vc",
        email="ops@terrace.vc",
        gstin="27AABCT0011E1Z6",
        address="Lower Parel, Mumbai 400013",
        notes="Clean quarterly subscription.",
        tags=["clean"],
    ),
]

# ---------------------------------------------------------------------------
# The active roster
#
# The §15 template is the default and stays exactly as it was. A generated roster
# can be swapped in for the duration of one ingestion, so a demo can be run on a
# company nobody has seen before without the transaction, contract and bank
# generators knowing anything about it — they read `roster.CUSTOMERS`, and that is
# resolved here, per context.
#
# A ContextVar rather than a module global: two workspaces can ingest concurrently,
# and a rebound global would let one run's companies leak into the other's evidence.
# PEP 562 module-level __getattr__ keeps the existing `roster.CUSTOMERS` call sites
# working unchanged, which is the point — the generators must stay ignorant of this.
# ---------------------------------------------------------------------------

_active: ContextVar[tuple[SyntheticCustomer, ...] | None] = ContextVar(
    "active_roster", default=None
)


def __getattr__(name: str):  # pragma: no cover - trivial dispatch
    if name == "CUSTOMERS":
        return list(_active.get() or _TEMPLATE)
    if name == "BY_KEY":
        return {c.key: c for c in (_active.get() or _TEMPLATE)}
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


@contextmanager
def use_roster(customers: Sequence[SyntheticCustomer] | None):
    """Make `customers` the roster every synthetic generator sees, for this block."""
    if customers is None:
        yield list(_TEMPLATE)
        return
    token = _active.set(tuple(customers))
    try:
        yield list(customers)
    finally:
        _active.reset(token)


def template() -> list[SyntheticCustomer]:
    """The §15 roster, whatever is currently active."""
    return list(_TEMPLATE)


def get(key: str) -> SyntheticCustomer:
    return {c.key: c for c in (_active.get() or _TEMPLATE)}[key]


def with_tag(tag: str) -> list[SyntheticCustomer]:
    return [c for c in (_active.get() or _TEMPLATE) if tag in c.tags]
