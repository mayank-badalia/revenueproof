"""Evidence-grounded explanation and reviewer packet — F6 sub-feature 6.

Every finding gets a packet built by code: the rule that fired, the observed value,
the baseline it was compared against, the records involved, the graph path where
there is one, the limitations, and the check a human should perform. That packet is
the finding. It is reproducible, it needs no model, and it is what Feature 7 and the
audit log store.

The Anomaly Explanation Agent adds prose *on top of* that packet, for material
findings only, and is given the packet rather than the raw evidence — so it can
restate and connect, but has nothing to invent from. If the model is unavailable,
over quota, or returns something unusable, the packet stands alone and the run
reports that the narrative is missing rather than quietly omitting it.

The wording constraint is absolute and is enforced here rather than trusted to the
prompt: core_resoruces.md requires findings to read "anomaly indicator, requires
review" and never "fraud". A narrative containing an accusatory term is discarded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from app.core.events import EventKind, Severity, emit
from app.core.llm import LLMUnavailableError, Role, is_available, structured_call
from app.models.enums import AnomalySeverity

from .rules import Finding

#: Terms a finding may never contain. These are investigation prompts; an
#: accusation of fraud is a legal conclusion this product is not entitled to draw.
FORBIDDEN = re.compile(
    r"\b(fraud|fraudulent|fraudster|criminal|illegal|launder\w*|embezzl\w*|"
    r"theft|stole|scam|deceit)\b",
    re.IGNORECASE,
)

#: Only findings at or above this severity are worth spending model budget on.
NARRATIVE_SEVERITIES = {AnomalySeverity.HIGH}

#: Hard cap per run, in the spirit of the Feature 2 critic budget. The provider's
#: request budget
#: is the binding constraint on any run that touches many records.
NARRATIVE_BUDGET = 8


class NarrativeOut(BaseModel):
    """What the model is allowed to produce — three short, grounded paragraphs."""

    summary: str = Field(description="One sentence restating what was observed.")
    why_it_matters: str = Field(description="Why a reviewer should care, given the baseline.")
    what_would_resolve_it: str = Field(description="The specific evidence that would settle it.")


@dataclass
class Packet:
    """The reviewer-facing record of one finding."""

    finding: Finding
    narrative: dict[str, str] | None = None
    narrative_status: str = "not_attempted"
    caveats: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        body = self.finding.as_dict()
        body["narrative"] = self.narrative
        body["narrative_status"] = self.narrative_status
        return body


def build_packet(finding: Finding) -> Packet:
    """The deterministic packet — always available, always the authority."""
    packet = Packet(finding=finding, caveats=list(finding.caveats))
    if not finding.baseline_value:
        # A rule that cannot say what normal looks like has not made an argument.
        packet.caveats.append(
            "No baseline was recorded for this rule, so the observation stands "
            "without a comparison."
        )
    packet.caveats.append(
        "This is an anomaly indicator requiring review. It is not a finding of "
        "wrongdoing."
    )
    return packet


def _prompt(finding: Finding) -> str:
    return (
        "You are summarising a due-diligence anomaly indicator for a reviewer.\n"
        "Use ONLY the facts below. Do not introduce numbers, names, dates or "
        "conclusions that are not present. Do not speculate about intent.\n"
        "This is an indicator requiring review, never an accusation of wrongdoing.\n\n"
        f"Rule: {finding.rule_id} — {finding.title}\n"
        f"Observed: {finding.observed_value}\n"
        f"Baseline: {finding.baseline_value}\n"
        f"Detail: {finding.explanation}\n"
        f"Required check: {finding.required_check}\n"
        f"Known limitations: {'; '.join(finding.caveats) or 'none recorded'}\n"
        f"Records involved: {len(finding.related_records)}\n"
    )


async def add_narratives(
    packets: list[Packet], *, workspace_id: str, budget: int = NARRATIVE_BUDGET
) -> dict[str, Any]:
    """Attach model prose to material findings, within budget.

    Returns a report of what the model did, so a run that skipped narratives says
    so instead of leaving a reader to assume none were warranted.
    """
    # `failed` is counted separately, and the totals are asserted to add up before
    # returning. Without it a call that errored simply vanished: seven material
    # findings could report "6 written, 0 rejected, 0 skipped", and the missing one
    # was invisible — a figure that does not reconcile, on a page whose whole claim
    # is that its figures reconcile.
    report: dict[str, Any] = {
        "eligible": 0, "attempted": 0, "written": 0,
        "rejected": 0, "skipped": 0, "failed": 0, "reason": "",
    }
    eligible = [p for p in packets if p.finding.severity in NARRATIVE_SEVERITIES]
    report["eligible"] = len(eligible)

    if not is_available():
        report["reason"] = "no model configured — packets stand without narrative"
        for packet in eligible:
            packet.narrative_status = "model_unavailable"
        report["skipped"] = len(eligible)
        return report

    for packet in eligible:
        if report["attempted"] >= budget:
            packet.narrative_status = "over_budget"
            report["skipped"] += 1
            continue
        report["attempted"] += 1
        try:
            result = await structured_call(
                role=Role.PROPOSER,
                system_prompt=(
                    "You restate due-diligence anomaly indicators for a reviewer. "
                    "You never accuse anyone of wrongdoing and never introduce facts."
                ),
                user_prompt=_prompt(packet.finding),
                schema=NarrativeOut,
                workspace_id=workspace_id,
                feature=6,
                max_completion_tokens=400,
            )
            written = result.parsed.model_dump()
        except (LLMUnavailableError, ValueError) as exc:
            packet.narrative_status = "failed"
            report["failed"] += 1
            report["reason"] = str(exc)[:160]
            continue

        combined = " ".join(str(v) for v in written.values())
        if FORBIDDEN.search(combined):
            # Enforced, not merely requested. A model that reaches for "fraud"
            # gets discarded rather than edited.
            packet.narrative_status = "rejected_wording"
            report["rejected"] += 1
            emit(
                EventKind.ERROR,
                f"Anomaly narrative rejected for accusatory wording ({packet.finding.rule_id})",
                workspace_id=workspace_id,
                severity=Severity.WARNING,
                feature=6,
            )
            continue

        packet.narrative = {k: str(v) for k, v in written.items()}
        packet.narrative_status = "written"
        report["written"] += 1

    accounted = (
        report["written"] + report["rejected"] + report["skipped"] + report["failed"]
    )
    if accounted != len(eligible):  # pragma: no cover - guards a counting mistake
        report["reason"] = (
            f"{accounted} of {len(eligible)} material findings accounted for — the "
            f"narrative tally does not reconcile and should not be read as complete"
        )
        return report
    if not report["reason"]:
        report["reason"] = f"{report['written']} of {len(eligible)} material findings narrated"
    return report
