"""The downloadable report — the evidence chain in one shareable file.

A due-diligence conversation happens over email, not over a localhost URL. The
report therefore has to survive being sent to someone who will never log in, and
still be checkable: every figure it states carries the rule that produced it and the
evidence ids behind it, so a reader who doubts a number has somewhere to look.

Self-contained HTML rather than PDF. A PDF would need a rendering engine in the
image and would lose the ability to link an item to its evidence; an HTML file opens
in any browser, prints to PDF from there, and can be diffed between versions — which
matters when the interesting question is *why did this number move*.

Nothing here computes. Every amount is read from what Feature 5 and Feature 6
already produced and stored, because a report that recalculates is a second
implementation that can disagree with the first.
"""

from __future__ import annotations

import html
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.money import format_money
from app.models import Anomaly, Contract, RevenueItem, Workspace
from app.models.enums import RevenueClass

CLASS_LABEL = {
    RevenueClass.VERIFIED_RECURRING: "Verified recurring",
    RevenueClass.VERIFIED_ONE_TIME: "Verified one-time",
    RevenueClass.CONTRACTED_UNPAID: "Contracted, unbilled",
    RevenueClass.INVOICED_UNPAID: "Invoiced, unpaid",
    RevenueClass.REFUNDED_OR_REVERSED: "Refunded / reversed",
    RevenueClass.PAYMENT_WITHOUT_SUPPORT: "Cash without support",
    RevenueClass.UNSUPPORTED_CLAIM: "Unsupported",
    RevenueClass.HUMAN_REVIEW: "Needs review",
}


def _money(minor: int, currency: str) -> str:
    # One formatter for the whole product: a report grouping a rupee figure
    # differently from the screen it came from is a report a reader has to
    # reconcile before they can read it.
    return f"{currency} {format_money(minor, currency)}"


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


async def build_report(
    session: AsyncSession, *, workspace_id: uuid.UUID
) -> tuple[str, str]:
    """Return `(filename, html)` for this workspace's current evidence position."""
    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        raise ValueError("workspace not found")

    currency = workspace.base_currency
    items = list(
        (
            await session.execute(
                select(RevenueItem)
                .where(RevenueItem.workspace_id == workspace_id)
                .order_by(RevenueItem.recognized_amount.desc())
            )
        )
        .scalars()
        .all()
    )
    anomalies = list(
        (
            await session.execute(
                select(Anomaly).where(Anomaly.workspace_id == workspace_id)
            )
        )
        .scalars()
        .all()
    )
    contracts = list(
        (
            await session.execute(
                select(Contract).where(Contract.workspace_id == workspace_id)
            )
        )
        .scalars()
        .all()
    )

    by_class: dict[str, list[RevenueItem]] = {}
    for item in items:
        by_class.setdefault(str(item.classification), []).append(item)

    verified = sum(
        i.recognized_amount
        for i in items
        if RevenueClass(i.classification).counts_as_verified
    )
    unread = sum(
        1 for c in contracts if c.recurring_amount == 0 and c.one_time_amount == 0
    )
    severity_rank = {"high": 0, "medium": 1, "low": 2, "info": 3}
    anomalies.sort(key=lambda a: (severity_rank.get(str(a.severity), 9), a.rule_id))

    generated = datetime.now(UTC).strftime("%d %B %Y at %H:%M UTC")
    filename = (
        f"revenueproof-{_slug(workspace.company_name)}-"
        f"{datetime.now(UTC).strftime('%Y%m%d')}.html"
    )

    rows = "\n".join(
        f"""<tr>
          <td>{_esc(item.description)}</td>
          <td><span class="tag">{_esc(CLASS_LABEL.get(RevenueClass(item.classification), item.classification))}</span></td>
          <td class="num">{_esc(_money(item.gross_amount, item.currency))}</td>
          <td class="num strong">{_esc(_money(item.recognized_amount, item.currency)) if item.recognized_amount else "—"}</td>
          <td class="rule">{_esc(item.rule_id)}<div class="why">{_esc(item.rule_explanation)}</div>
            {"<div class='missing'>Missing: " + _esc(", ".join(item.missing_evidence)) + "</div>" if item.missing_evidence else ""}
            {"<div class='ids'>" + _esc(" · ".join(item.evidence_ids[:6])) + "</div>" if item.evidence_ids else ""}
          </td>
        </tr>"""
        for item in items
    )

    findings = "\n".join(
        f"""<div class="finding sev-{_esc(a.severity)}">
          <div class="fhead"><strong>{_esc(a.title)}</strong>
            <span class="sev">{_esc(a.severity)}</span>
            <code>{_esc(a.rule_id)}</code>
            {"<span class='fp'>marked not useful</span>" if a.is_false_positive else ""}
          </div>
          <div class="obs"><span>Observed:</span> {_esc(a.observed_value)}
            &nbsp;&nbsp;<span>Baseline:</span> {_esc(a.baseline_value) or "not recorded"}</div>
          <p>{_esc(a.explanation)}</p>
          <p class="check"><span>What to check:</span> {_esc(a.required_check)}</p>
          {"<ul class='caveats'>" + "".join(f"<li>{_esc(c)}</li>" for c in a.caveats) + "</ul>" if a.caveats else ""}
        </div>"""
        for a in anomalies
    )

    summary_rows = "\n".join(
        f"<tr><td>{_esc(CLASS_LABEL.get(RevenueClass(name), name))}</td>"
        f"<td class='num'>{len(group)}</td>"
        f"<td class='num'>{_esc(_money(sum(i.recognized_amount for i in group), currency))}</td></tr>"
        for name, group in sorted(by_class.items())
    )

    return filename, _TEMPLATE.format(
        company=_esc(workspace.company_name),
        period=f"{workspace.reporting_period_start} to {workspace.reporting_period_end}",
        generated=generated,
        claimed=_esc(_money(workspace.claimed_revenue, currency)),
        claimed_arr=_esc(_money(workspace.claimed_arr, currency)),
        verified=_esc(_money(verified, currency)),
        gap=_esc(_money(max(0, workspace.claimed_revenue - verified), currency)),
        item_count=len(items),
        anomaly_count=len(anomalies),
        high_count=sum(1 for a in anomalies if str(a.severity) == "high"),
        unread_note=(
            f"<p class='warn'>{unread} of {len(contracts)} contracts have not been "
            f"read, so supported ARR reflects only the contracts already extracted.</p>"
            if unread
            else ""
        ),
        summary_rows=summary_rows,
        rows=rows,
        findings=findings or "<p class='muted'>No anomaly indicators were raised.</p>",
        policy_version=_esc(items[0].policy_version if items else "v1"),
    )


def _slug(name: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "-" for ch in name).strip("-")[:40]


_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RevenueProof — {company}</title>
<style>
  :root {{ --ink:#0f172a; --muted:#64748b; --line:#e2e8f0; --ok:#047857; --warn:#b45309; --bad:#b91c1c; }}
  * {{ box-sizing:border-box; }}
  body {{ font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
         color:var(--ink); margin:0; padding:32px; background:#fff; }}
  .wrap {{ max-width:920px; margin:0 auto; }}
  h1 {{ font-size:21px; margin:0 0 4px; }}
  h2 {{ font-size:15px; margin:32px 0 10px; padding-bottom:6px; border-bottom:1px solid var(--line); }}
  .sub {{ color:var(--muted); font-size:12px; margin:0 0 24px; }}
  .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:12px; }}
  .card {{ border:1px solid var(--line); border-radius:8px; padding:12px; }}
  .card .k {{ font-size:11px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); }}
  .card .v {{ font-size:18px; font-weight:600; margin-top:4px; font-variant-numeric:tabular-nums; }}
  .v.ok {{ color:var(--ok); }} .v.gap {{ color:var(--warn); }}
  table {{ width:100%; border-collapse:collapse; margin-top:8px; font-size:12.5px; }}
  th,td {{ text-align:left; padding:7px 8px; border-bottom:1px solid var(--line); vertical-align:top; }}
  th {{ color:var(--muted); font-weight:500; font-size:11px; text-transform:uppercase; }}
  .num {{ text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }}
  .strong {{ font-weight:600; }}
  .tag {{ background:#f1f5f9; border-radius:4px; padding:1px 6px; font-size:11px; white-space:nowrap; }}
  .rule {{ font-size:11px; color:var(--muted); max-width:320px; }}
  .rule .why {{ color:var(--ink); margin-top:2px; }}
  .missing {{ color:var(--warn); margin-top:2px; }}
  .ids {{ font-family:ui-monospace,SFMono-Regular,monospace; font-size:10px; margin-top:2px; word-break:break-all; }}
  .finding {{ border:1px solid var(--line); border-left-width:3px; border-radius:6px; padding:10px 12px; margin-bottom:10px; }}
  .sev-high {{ border-left-color:var(--bad); }} .sev-medium {{ border-left-color:var(--warn); }}
  .sev-low {{ border-left-color:var(--line); }}
  .fhead {{ display:flex; gap:8px; align-items:center; flex-wrap:wrap; }}
  .fhead code {{ font-size:10px; color:var(--muted); }}
  .sev {{ font-size:10px; text-transform:uppercase; background:#f1f5f9; border-radius:4px; padding:1px 5px; }}
  .fp {{ font-size:10px; background:#f1f5f9; border-radius:4px; padding:1px 5px; color:var(--muted); }}
  .obs {{ font-size:11.5px; margin-top:5px; }} .obs span {{ color:var(--muted); }}
  .finding p {{ margin:6px 0 0; font-size:12.5px; }}
  .check span {{ color:var(--muted); }}
  .caveats {{ margin:6px 0 0; padding-left:18px; color:var(--muted); font-size:11.5px; }}
  .warn {{ background:#fffbeb; border:1px solid #fde68a; border-radius:6px; padding:8px 10px; font-size:12px; }}
  .note {{ background:#f8fafc; border:1px solid var(--line); border-radius:6px; padding:10px 12px;
           font-size:11.5px; color:var(--muted); margin-top:28px; }}
  .muted {{ color:var(--muted); }}
  @media print {{ body {{ padding:0; }} .finding {{ break-inside:avoid; }} }}
</style></head><body><div class="wrap">

<h1>RevenueProof — {company}</h1>
<p class="sub">Reporting period {period} · generated {generated} · policy {policy_version}</p>

<div class="cards">
  <div class="card"><div class="k">Claimed revenue</div><div class="v">{claimed}</div></div>
  <div class="card"><div class="k">Evidence-supported (before review)</div><div class="v ok">{verified}</div></div>
  <div class="card"><div class="k">Not evidenced</div><div class="v gap">{gap}</div></div>
  <div class="card"><div class="k">Claimed ARR</div><div class="v">{claimed_arr}</div></div>
</div>
{unread_note}

<h2>Revenue by classification</h2>
<table><thead><tr><th>State</th><th class="num">Items</th><th class="num">Recognised</th></tr></thead>
<tbody>{summary_rows}</tbody></table>

<h2>Classified items ({item_count})</h2>
<table><thead><tr><th>Item</th><th>Classification</th><th class="num">Gross</th>
<th class="num">Recognised</th><th>Rule and evidence</th></tr></thead>
<tbody>{rows}</tbody></table>

<h2>Anomaly indicators ({anomaly_count}, {high_count} high)</h2>
{findings}

<div class="note">
  <strong>What this report is.</strong> It states what the collected evidence
  supports, item by item, with the rule and the evidence ids behind every figure.
  Every anomaly listed is an <em>indicator requiring review</em> — none of them is a
  finding of wrongdoing, and none asserts that anyone acted improperly.
  RevenueProof does not give investment advice and does not certify revenue. Amounts
  follow RevenueProof's stated revenue policy, which is versioned above and is not
  an accounting standard.
</div>

</div></body></html>
"""
