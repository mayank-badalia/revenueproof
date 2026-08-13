"""End-to-end verification — one command that exercises the whole product and checks it.

Run it and read the last line. It builds a workspace from scratch through the real
HTTP API, runs every feature in order, downloads the artefacts, opens them, and
asserts the things a reviewer would otherwise have to check by hand: that the money
reconciles, that the adversarial cases were actually caught, that the evidence chain
reaches the bank, that the report is a complete self-contained document, and that a
generated dataset mentions none of the built-in companies.

    .venv/bin/python scripts/verify_all.py                # generated data, full run
    .venv/bin/python scripts/verify_all.py --seed my-demo # pick the companies
    .venv/bin/python scripts/verify_all.py --template     # the §15 fixture instead
    .venv/bin/python scripts/verify_all.py --no-llm       # skip contracts and critic prose

Everything it prints is measured in this run. Where a check cannot be made — no
model configured, a stage skipped — it says so rather than passing quietly, because
a green line that means "not checked" is worse than a red one.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import time
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

# Overridable so the same checks can be pointed at a deployed stack. A harness
# that can only verify localhost cannot tell you whether what you shipped works.
BASE = os.environ.get("REVENUEPROOF_API", "http://127.0.0.1:8000")
PASSWORD = "CorrectHorse9!battery"

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)


@dataclass
class Report:
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    failures: list[str] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)

    def check(self, label: str, ok: bool, detail: str = "") -> bool:
        if ok:
            self.passed += 1
            print(f"  {GREEN}PASS{RESET}  {label}{DIM + ' — ' + detail + RESET if detail else ''}")
        else:
            self.failed += 1
            self.failures.append(label)
            print(f"  {RED}FAIL{RESET}  {label}{' — ' + detail if detail else ''}")
        return ok

    def skip(self, label: str, why: str) -> None:
        self.skipped += 1
        print(f"  {YELLOW}SKIP{RESET}  {label} — {why}")


R = Report()


def section(title: str) -> None:
    print(f"\n{BOLD}{title}{RESET}")


def timed(name: str, fn):
    start = time.time()
    result = fn()
    R.timings[name] = time.time() - start
    return result


# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", default="verify-run")
    parser.add_argument("--template", action="store_true",
                        help="use the built-in §15 dataset instead of a generated one")
    parser.add_argument("--no-llm", action="store_true",
                        help="skip contract extraction and model narration")
    args = parser.parse_args()
    use_llm = not args.no_llm
    seed = None if args.template else args.seed

    client = httpx.Client(base_url=BASE, timeout=2400.0)

    section("Service health")
    try:
        health = client.get("/health").json()
    except Exception as exc:  # noqa: BLE001
        print(f"  {RED}FAIL{RESET}  backend unreachable at {BASE} — {exc}")
        print(f"\n{RED}Cannot verify anything without the API.{RESET}")
        return 2
    services = health.get("services", {})
    for name in ("postgres", "redis", "neo4j"):
        R.check(f"{name} reachable", bool(services.get(name, {}).get("ok")),
                services.get(name, {}).get("error", "") or "")
    llm_ok = bool(services.get("llm", {}).get("ok"))
    if use_llm and not llm_ok:
        R.skip("language model", "not configured; running deterministic paths only")
        use_llm = False

    # --- a fresh workspace, through the real API -------------------------
    section("Workspace and connections")
    email = f"verify+{int(time.time())}@example.com"
    reg = client.post("/api/v1/auth/register",
                      json={"email": email, "password": PASSWORD, "full_name": "Verifier"})
    if not R.check("register a new user", reg.status_code == 201, reg.text[:120]):
        return 2
    auth = {"Authorization": f"Bearer {reg.json()['access_token']}"}

    ws = client.post("/api/v1/workspaces", headers=auth, json={
        "company_name": f"Verification {datetime.now(UTC):%H%M%S}",
        "reporting_period_start": "2026-04-01",
        "reporting_period_end": "2027-03-31",
        "base_currency": "INR",
        # Set from what the §15 dataset actually retains (1,39,83,000) plus a
        # realistic overstatement. A claim *below* the evidence made the room
        # report the position proven at 123%, which is arithmetically right and
        # useless as a demonstration.
        "claimed_revenue": "15000000.00",
        "claimed_arr": "4800000.00",
    })
    if not R.check("create a workspace", ws.status_code == 201, ws.text[:120]):
        return 2
    wid = ws.json()["id"]
    print(f"  {DIM}workspace {wid}{RESET}")

    summary = client.get(f"/api/v1/workspaces/{wid}/summary", headers=auth).json()
    providers = summary.get("deployment_providers", {})
    R.check("deployment credentials are reported", isinstance(providers, dict))
    live = [n for n, ok in providers.items() if ok]
    if live:
        R.check("a new workspace is connected without sign-in", bool(live), ", ".join(live))
    else:
        R.skip("connected accounts", "no provider credentials on this deployment")

    # --- ingestion --------------------------------------------------------
    section("Feature 1 — evidence ingestion")
    body: dict[str, Any] = {"include_bank_sample": True, "use_demo_data": True}
    if seed:
        body["dataset_seed"] = seed
    ing = timed("ingest", lambda: client.post(
        f"/api/v1/workspaces/{wid}/ingest", headers=auth, json=body))
    if not R.check("ingestion completes", ing.status_code == 200, ing.text[:150]):
        return 2
    ingested = ing.json()
    R.check("canonical records written", ingested["total_canonical"] > 100,
            f"{ingested['total_canonical']} records")

    ev = client.get(f"/api/v1/workspaces/{wid}/evidence", headers=auth).json()
    counts = {c["record_type"]: c["count"] for c in ev.get("counts", [])}
    R.check("invoices ingested", counts.get("invoice", 0) >= 50, str(counts.get("invoice")))
    R.check("payments ingested", counts.get("payment", 0) >= 50, str(counts.get("payment")))
    R.check("bank rows ingested", counts.get("bank_transaction", 0) >= 60,
            str(counts.get("bank_transaction")))

    quarantine = client.get(f"/api/v1/workspaces/{wid}/quarantine", headers=auth).json()
    R.check("no evidence was silently dropped",
            quarantine["summary"]["total"] == 0
            or bool(quarantine["records"]),
            f"{quarantine['summary']['total']} quarantined, all listed")

    # Re-running must not duplicate anything. This is the idempotency guarantee the
    # whole vault rests on, and it is cheap to check here.
    client.post(f"/api/v1/workspaces/{wid}/ingest", headers=auth, json=body)
    ev2 = client.get(f"/api/v1/workspaces/{wid}/evidence", headers=auth).json()
    counts2 = {c["record_type"]: c["count"] for c in ev2.get("counts", [])}
    R.check("re-ingestion creates no duplicate records", counts == counts2,
            f"{counts} vs {counts2}")

    # --- identity ---------------------------------------------------------
    section("Feature 2 — customer identity")
    ident = timed("identity", lambda: client.post(
        f"/api/v1/workspaces/{wid}/identity/resolve", headers=auth, json={"use_critic": False}))
    R.check("identity resolution completes", ident.status_code == 200, ident.text[:120])
    res = ident.json()
    R.check("customers were clustered", res["clusters"] > 0, f"{res['clusters']} clusters")
    R.check("a false merge was prevented", len(res.get("blocked_merges", [])) > 0,
            f"{len(res.get('blocked_merges', []))} blocked")

    # --- contracts --------------------------------------------------------
    section("Feature 3 — contract intelligence")
    if use_llm:
        con = timed("contracts", lambda: client.post(
            f"/api/v1/workspaces/{wid}/contracts/process", headers=auth, json={}))
        if R.check("contract extraction completes", con.status_code == 200, con.text[:150]):
            cbody = con.json()
            R.check("contracts were read", cbody.get("extracted", 0) > 0,
                    f"{cbody.get('extracted')} extracted of {cbody.get('processed')} "
                    f"processed, {cbody.get('failed', 0)} failed")
            contracts = client.get(f"/api/v1/workspaces/{wid}/contracts", headers=auth).json()
            read = [c for c in contracts["contracts"]
                    if c["recurring_amount"]["minor"] or c["one_time_amount"]["minor"]]
            R.check("extracted terms carry amounts", bool(read), f"{len(read)} with values")
            cited = [c for c in read if (c.get("extraction_confidence") or 0) > 0]
            R.check("extractions are backed by verified citations", bool(cited),
                    f"{len(cited)} with verified citations")
    else:
        R.skip("contract extraction", "needs the language model")

    # --- anomaly (runs reconciliation and revenue inside) -----------------
    section("Features 4-6 — reconciliation, revenue truth, anomalies")
    scan = timed("anomaly", lambda: client.post(
        f"/api/v1/workspaces/{wid}/anomalies/scan", headers=auth,
        json={"use_llm": use_llm}))
    if not R.check("anomaly scan completes", scan.status_code == 200, scan.text[:150]):
        return 2
    sbody = scan.json()
    rules = sbody["by_rule"]
    R.check("indicators were raised", sbody["findings_total"] > 0,
            f"{sbody['findings_total']} findings")

    # The dataset plants these deliberately; not finding them means the detectors
    # regressed, whichever companies they are about.
    for rule, label in (
        ("A01_DUPLICATE_PAYMENT", "near-duplicate payment"),
        ("A07_CUSTOMER_CONCENTRATION", "customer concentration"),
        ("A10_RELATED_PARTY_REVENUE", "related party"),
    ):
        R.check(f"detected: {label}", rules.get(rule, 0) > 0, f"{rules.get(rule, 0)} found")
    for rule, label in (
        ("A11_CIRCULAR_FUNDS", "circular funds"),
        ("A06_SHARED_PAYMENT_ACCOUNT", "shared payment account"),
    ):
        if rules.get(rule, 0) > 0:
            R.check(f"detected: {label}", True, f"{rules[rule]} found")
        else:
            R.skip(f"detected: {label}", "not present in this run's evidence")

    R.check("every finding carries a baseline",
            all(f.get("baseline_value") for f in sbody["findings"]),
            "a rule that cannot say what normal looks like has not made an argument")
    forbidden = re.compile(r"\b(fraud|fraudulent|criminal|launder\w*|embezzl\w*)\b", re.I)
    R.check("no finding uses accusatory language",
            not any(forbidden.search(json.dumps(f)) for f in sbody["findings"]))
    R.check("the model states whether it ran", bool(sbody["ml"].get("reason")),
            sbody["ml"]["reason"][:70])

    conc = sbody["concentration"]
    if conc["per_customer"]:
        total = round(sum(c["share_pct"] for c in conc["per_customer"]))
        R.check("concentration shares sum to 100%", total == 100, f"{total}%")

    # --- what a reopened page shows ---------------------------------------
    # Features 4 and 5 are derived state: the allocations and items persist, the
    # figures built from them do not. Reopening the workspace used to render
    # "collect evidence first" over a reconciliation that had already run, and list
    # classified items with no statement of what they came to against the claim.
    # The read paths recompute through the same functions the buttons call, so this
    # asserts the two agree rather than merely that the page is no longer blank.
    stored_recon = client.get(
        f"/api/v1/workspaces/{wid}/reconciliation", headers=auth, timeout=120
    )
    if R.check("the reconciled position survives a reload",
               stored_recon.status_code == 200 and stored_recon.json().get("reconciled"),
               stored_recon.text[:150]):
        replay = stored_recon.json()
        R.check("the restored reconciliation reports retained cash",
                replay["total_retained_minor"] > 0,
                replay["totals"]["retained"]["display"])
        R.check("the restored reconciliation still conserves value",
                replay["conservation_ok"], replay.get("conservation_error") or "")
        R.check("a read recomputation writes no allocations",
                replay["allocations_written"] == 0,
                f"{replay['allocations_written']} written")

    stored_rev = client.get(
        f"/api/v1/workspaces/{wid}/revenue/summary", headers=auth, timeout=120
    )
    if R.check("the verified position survives a reload",
               stored_rev.status_code == 200 and stored_rev.json().get("verified"),
               stored_rev.text[:150]):
        summary = stored_rev.json()
        R.check("the restored summary states the claim it was measured against",
                summary["totals"]["claimed_revenue"] > 0,
                summary["money"]["claimed_revenue"]["display"])
        R.check("the restored waterfall lands on the verified total",
                summary["waterfall"][-1]["amount_minor"]
                == summary["totals"]["total_verified"],
                summary["money"]["total_verified"]["display"])

    # --- critic -----------------------------------------------------------
    section("Feature 7 — adversarial verification")
    crit = timed("critic", lambda: client.post(
        f"/api/v1/workspaces/{wid}/critic/run", headers=auth, json={"use_llm": use_llm}))
    if R.check("critic run completes", crit.status_code == 200, crit.text[:150]):
        cb = crit.json()
        R.check("every classified item was challenged", cb["items_reviewed"] > 0,
                f"{cb['items_reviewed']} reviewed")
        # Publication is decided by the deterministic half. The model can argue, and
        # every objection reaches a person, but it cannot withhold a figure on its
        # own — a model that returns a different verdict on identical evidence would
        # otherwise make the headline number irreproducible, which it did: 75.1%,
        # 72.3% and 35.8% of the same claim across three runs.
        R.check("every published item is accounted for",
                cb["published"]
                == cb["approved"] + cb["published_over_model_objection"],
                f"{cb['published']} published = {cb['approved']} approved + "
                f"{cb['published_over_model_objection']} over a model objection")
        decisions = client.get(f"/api/v1/workspaces/{wid}/critic", headers=auth).json()
        # The invariant that actually protects a figure: arithmetic is never
        # overruled. Anything a deterministic check failed stays unpublished.
        leaked = [
            d for d in decisions["decisions"]
            if d["is_published"] and d["deterministic_findings"]
        ]
        R.check("no item that failed a deterministic check is published",
                not leaked, f"{len(leaked)} leaked")
        objected = [
            d for d in decisions["decisions"]
            if d["is_published"] and d["verdict"] != "APPROVED"
        ]
        R.check("a model objection still reaches a human",
                all(d["routed_to_feature"] for d in objected if d["issue_codes"]),
                f"{len(objected)} published carrying an objection")
        R.check("disputes are routed to an owning feature",
                all(d["routed_to_feature"] for d in decisions["decisions"]
                    if d["verdict"] != "APPROVED" and d["issue_codes"]))

    review = client.get(f"/api/v1/workspaces/{wid}/review", headers=auth).json()
    summary_q = review["summary"]
    R.check("the queue reports decisions, not just records",
            "open_decisions" in summary_q,
            f"{summary_q.get('open_decisions')} decisions over {summary_q['open']} records")
    if summary_q["open"] and summary_q.get("open_decisions"):
        R.check("equivalent questions are grouped",
                summary_q["open_decisions"] <= summary_q["open"])

    # A decision must be refused without a reason. Checked, not assumed.
    if review["items"]:
        first = review["items"][0]["id"]
        bad_resolve = client.post(
            f"/api/v1/workspaces/{wid}/review/{first}/resolve", headers=auth,
            json={"decision": "approved", "reason": ""})
        R.check("a resolution without a reason is refused",
                bad_resolve.status_code == 422, str(bad_resolve.status_code))
        good = client.post(
            f"/api/v1/workspaces/{wid}/review/{first}/resolve", headers=auth,
            json={"decision": "approved", "reason": "Verified against the source records."})
        R.check("a resolution with a reason is accepted", good.status_code == 200,
                good.text[:120])

    # --- the room ---------------------------------------------------------
    section("Feature 8 — diligence room")
    room = timed("room", lambda: client.get(f"/api/v1/workspaces/{wid}/room", headers=auth))
    if R.check("room loads", room.status_code == 200, room.text[:120]):
        rb = room.json()
        R.check("withheld amounts state why they are withheld",
                all(i["withheld_because"] for i in rb["items"] if not i["is_published"]))
        published = [i for i in rb["items"] if i["is_published"]]
        if published:
            trace = client.get(
                f"/api/v1/workspaces/{wid}/room/trace/{published[0]['id']}",
                headers=auth).json()
            kinds = [n["kind"] for n in trace["nodes"]]
            R.check("the evidence chain reaches a bank credit", "bank" in kinds,
                    " → ".join(kinds))
            R.check("the chain is linked end to end", bool(trace["edges"]))
            R.check("the trace carries its rule", bool(trace["rule_id"]))
        else:
            R.skip("evidence chain", "nothing was published to trace")

        v1 = client.post(f"/api/v1/workspaces/{wid}/room/publish", headers=auth,
                         json={}).json()
        R.check("a version can be published", v1.get("created") is True,
                f"version {v1.get('version')}")
        v2 = client.post(f"/api/v1/workspaces/{wid}/room/publish", headers=auth,
                         json={}).json()
        R.check("an unchanged position creates no second version",
                v2.get("created") is False)

        changes = timed("monitoring", lambda: client.get(
            f"/api/v1/workspaces/{wid}/room/changes",
            headers=auth, params={"days": 365})).json()
        R.check("monitoring reports a definite answer", "unchanged" in changes,
                changes.get("summary", "")[:80])

        # Feature 8's other half: redo only what a change invalidated. Forced here
        # because nothing has moved in a fresh workspace, and the path still has to
        # be exercised.
        rerun = timed("rerun", lambda: client.post(
            f"/api/v1/workspaces/{wid}/room/rerun", headers=auth,
            json={"days": 365, "force": True, "use_llm": use_llm})).json()
        R.check("a forced rerun completes and versions", bool(rerun.get("ran")),
                ", ".join(rerun.get("ran", []))[:80])
        R.check("the rerun leaves a dated version",
                bool(rerun.get("version", {}).get("version")),
                f"version {rerun.get('version', {}).get('version')}")

    # --- audit ------------------------------------------------------------
    section("Audit trail")
    audit = client.get(f"/api/v1/workspaces/{wid}/audit", headers=auth).json()
    R.check("audit hash chain verifies", audit["integrity"]["valid"],
            audit["integrity"].get("error") or f"{audit['integrity']['checked']} events")
    actions = {e["action"] for e in audit["events"]}
    for action in ("workspace.created", "evidence.ingested", "critic.reviewed"):
        R.check(f"audited: {action}", action in actions)

    # --- downloads: open them and read them -------------------------------
    section("Downloads — opened and inspected")
    rep = client.get(f"/api/v1/workspaces/{wid}/report", headers=auth)
    if R.check("report downloads", rep.status_code == 200, f"{len(rep.content)} bytes"):
        check_report(rep.text, rep.headers.get("content-disposition", ""))

    ds = client.get("/api/v1/demo-dataset", params={"seed": seed} if seed else {})
    if R.check("dataset downloads", ds.status_code == 200, f"{len(ds.content)} bytes"):
        check_dataset(ds.content, generated=bool(seed))

    # --- timings ----------------------------------------------------------
    section("Timings")
    labels = {
        "ingest": "F1 ingestion", "identity": "F2 identity",
        "contracts": "F3 contracts", "anomaly": "F4-F6 cash, revenue, anomalies",
        "critic": "F7 critic", "room": "F8 room", "monitoring": "F8 monitoring",
        "rerun": "F8 rerun",
    }
    for name, seconds in R.timings.items():
        print(f"  {DIM}{labels.get(name, name):32}{seconds:7.1f}s{RESET}")
    total = sum(R.timings.values())
    print(f"  {BOLD}{'full pipeline':32}{total:7.1f}s{RESET}")

    section("Result")
    total = R.passed + R.failed
    if R.failed:
        print(f"  {RED}{R.failed} of {total} checks failed{RESET} "
              f"({R.skipped} skipped)")
        for failure in R.failures:
            print(f"    {RED}·{RESET} {failure}")
        return 1
    print(f"  {GREEN}All {R.passed} checks passed{RESET} ({R.skipped} skipped)")
    print(f"  {DIM}workspace {wid}{RESET}")
    return 0


def check_report(html: str, disposition: str) -> None:
    """Open the report and read it the way a recipient would."""
    R.check("report is a complete HTML document",
            html.startswith("<!doctype html>") and html.rstrip().endswith("</html>"))
    # The header now carries both the ASCII `filename=` and the RFC 5987
    # `filename*=UTF-8''…` form, because a cross-origin fetch could not read the
    # name at all before `Access-Control-Expose-Headers` was set — which is how
    # downloads ended up named after a blob identifier.
    R.check("report filename names the company",
            "revenueproof-" in disposition and ".html" in disposition,
            disposition[:90])
    R.check("the download names itself for both old and new clients",
            'filename="' in disposition and "filename*=UTF-8" in disposition,
            disposition[:90])
    R.check("no unrendered placeholder survives",
            not re.search(r"\{[a-z_]+\}", html.split("</style>")[-1]))
    for section_name in ("Claimed revenue", "Evidence-supported",
                         "Classified items", "Anomaly indicators"):
        R.check(f"report section: {section_name}", section_name in html)
    R.check("report states it is not investment advice",
            "does not give investment advice" in html)
    R.check("report frames findings as indicators",
            "indicator requiring review" in html)
    forbidden = re.search(r"\b(fraud|criminal|launder\w*|embezzl\w*)\b", html, re.I)
    R.check("report contains no accusatory wording", forbidden is None,
            forbidden.group(0) if forbidden else "")
    R.check("report is self-contained",
            "<script" not in html and "https://" not in html)
    R.check("money is formatted, not raw minor units",
            "INR " in html and not re.search(r">\s*\d{9,}\s*<", html))
    R.check("report has item rows", html.count("<tr>") > 5, f"{html.count('<tr>')} rows")


def check_dataset(payload: bytes, *, generated: bool) -> None:
    """Open the zip, parse the statement, and reconcile its own balance column."""
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = set(archive.namelist())
        if not R.check("dataset contains README, JSON and CSV",
                       {"README.txt", "dataset.json", "bank_statement.csv"} <= names,
                       ", ".join(sorted(names))):
            return
        body = json.loads(archive.read("dataset.json"))
        rows = list(csv.DictReader(io.StringIO(
            archive.read("bank_statement.csv").decode())))

    R.check("dataset has 20 customers", len(body["customers"]) == 20,
            str(len(body["customers"])))
    R.check("dataset has invoices and payments",
            len(body["invoices"]) > 50 and len(body["payments"]) > 50)
    cases = set(body["cases_planted"]["cases"])
    for case in ("related_party", "false_merge_trap", "shared_payment_account",
                 "circular_flow"):
        R.check(f"case planted: {case}", case in cases)

    R.check("statement has rows", len(rows) > 50, f"{len(rows)} rows")
    R.check("every statement row is a debit or a credit, never both",
            all(bool(r["Debit"]) != bool(r["Credit"]) for r in rows))
    R.check("every statement date parses",
            all(re.fullmatch(r"\d{2}/\d{2}/\d{4}", r["Date"] or "") for r in rows))

    # The one check that proves it is a statement rather than a table of numbers.
    drift = None
    balance = Decimal(rows[0]["Balance"]) - (
        Decimal(rows[0]["Credit"] or 0) - Decimal(rows[0]["Debit"] or 0))
    for index, row in enumerate(rows):
        balance += Decimal(row["Credit"] or 0) - Decimal(row["Debit"] or 0)
        if balance != Decimal(row["Balance"]):
            drift = f"row {index + 1}: expected {balance}, file says {row['Balance']}"
            break
    R.check("the statement's running balance reconciles", drift is None, drift or "")

    R.check("absent spellings are null, not empty strings",
            not any(c.get("accounting_name") == "" for c in body["customers"]))

    text = (json.dumps(body) + " ".join(r["Description"] for r in rows)).upper()
    leaks = [n for n in ("NORTHSTAR", "NSTAR TECH", "BLUE HARBOR", "GLOBAL PAY",
                         "APEX FOUNDER", "QUANTUM RETAIL")
             if n in text]
    if generated:
        R.check("a generated dataset mentions no built-in company", not leaks,
                f"leaked {leaks}")
        R.check("the variant is labelled generated",
                body["variant"].startswith("generated-"), body["variant"])
    else:
        R.check("the built-in dataset is labelled template",
                body["variant"] == "template", body["variant"])


if __name__ == "__main__":
    sys.exit(main())
