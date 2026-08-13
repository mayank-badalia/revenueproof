"""Match Critic Agent — Feature 2, sub-feature 5.

An independent agent that tries to *disprove* a proposed identity link.

Three design rules, all from core_resoruces.md:

* **Deterministic checks run first.** Contradictory tax IDs or domains settle the
  question without an LLM. Asking a model to re-litigate a fact that code already
  established adds cost, latency and a chance of being talked out of the right answer.
* **The critic sees original identifiers, not the proposer's summary.** It is given
  the raw names, domains, tax IDs and addresses of both records — otherwise it is
  reviewing an argument rather than the evidence.
* **The critic uses a different model family** from any proposing agent. Model
  agreement between two instances of the same model is not independent verification.

The critic can only ever make a link *weaker* or send it to review. It cannot
promote a rejected pair to accepted: an LLM must not be able to talk the system into
a merge that the deterministic evidence argued against.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.core import llm
from app.core.events import EventKind, Severity, emit
from app.features.identity.matching import MatchCandidate


class CriticVerdictModel(BaseModel):
    """Structured critic output. Free-form verdicts are rejected."""

    verdict: Literal["AGREE", "DISPUTE", "NEEDS_REVIEW"]
    # Issue codes make disputes countable and comparable across runs.
    # Kept short deliberately: the schema is serialised into every system prompt,
    # so each extra literal costs tokens on every single call.
    issue_codes: list[
        Literal[
            "DIFFERENT_LEGAL_ENTITY",
            "SHARED_ADDRESS_OR_AGENT",
            "INSUFFICIENT_EVIDENCE",
            "CONTRADICTORY_IDENTIFIER",
            "NONE",
        ]
    ] = Field(default_factory=list)
    reasoning: str = Field(max_length=400)


CRITIC_SYSTEM_PROMPT = """\
You are an independent identity critic for financial due diligence. Two records have \
been proposed as the SAME customer. Try to DISPROVE it.

A false merge is worse than a missed match: it understates customer concentration and \
can conceal a related party.

DISPUTE = plausibly different legal entities.
NEEDS_REVIEW = genuinely insufficient evidence either way.
AGREE = the identifiers make one entity clearly the best reading.

Traps: near-identical names with different tax registrations are DIFFERENT; a parent \
and subsidiary share domain/address but are DIFFERENT; customers behind one payment \
agent are DIFFERENT; a shared office address is shared premises, not identity.

Judge only the identifiers given. Do not invent evidence.\
"""


def _describe(candidate: MatchCandidate) -> str:
    """Render both records' original identifiers, compactly.

    Kept terse on purpose. The free-tier budget is 8,000 tokens per minute, so a
    verbose prompt directly reduces how many links can be challenged in a run. Empty
    fields are omitted rather than printed as "(none)", and only the three
    highest-magnitude evidence items are included — the rest never change a verdict.
    """

    def side(keys, label: str) -> str:
        parts = [f"{label}: {keys.display_name} [{keys.source_system}/{keys.record_type}]"]
        if keys.tax_ids:
            parts.append(f"tax={','.join(sorted(keys.tax_ids))}")
        if keys.pans:
            parts.append(f"pan={','.join(sorted(keys.pans))}")
        if keys.domains:
            parts.append(f"domain={','.join(sorted(keys.domains))}")
        if keys.emails:
            parts.append(f"email={','.join(sorted(keys.emails))}")
        if keys.addresses:
            parts.append(f"addr={sorted(keys.addresses)[0][:60]}")
        return " | ".join(parts)

    evidence = "; ".join(
        f"{item['field']}={item['outcome']}({item['weight']:+})"
        for item in candidate.explain()[:3]
    )
    return (
        f"{side(candidate.left, 'A')}\n"
        f"{side(candidate.right, 'B')}\n"
        f"Proposed: MERGE (weight {candidate.total_weight:+.1f})\n"
        f"Top evidence: {evidence}"
    )


# An ACCEPTED pair at or above this weight, with no conflicting signal, is treated
# as settled. THRESHOLD_ACCEPT is 6.0; this sits well clear of it, so only merges
# in the contested band reach the model.
OVERWHELMING_WEIGHT = 10.0


def is_material(candidate: MatchCandidate) -> bool:
    """Whether this link is worth an independent model challenge.

    core_resoruces.md scopes the critic to *material* decisions. For identity the
    material set is narrow: pairs where a merge is actually being proposed and the
    evidence is genuinely contestable. Everything else either goes to a human
    already or is not a judgement call at all.
    """
    if candidate.decision != "ACCEPTED":
        return False

    # A genuine contradiction always warrants a challenge, however strong the rest
    # of the evidence. Note this is specifically a `conflict` outcome — a
    # contradictory tax ID or domain — not merely a negative weight. Almost every
    # pair carries some negative signal (a missing token, a name below the top
    # band); treating those as contestable sent nearly every merge to the model and
    # split the customer list wide open.
    if any(comparison.outcome == "conflict" for comparison in candidate.comparisons):
        return True

    return candidate.total_weight < OVERWHELMING_WEIGHT


def deterministic_objections(candidate: MatchCandidate) -> list[str]:
    """Contradictions that settle a link without consulting a model."""
    objections: list[str] = []
    left, right = candidate.left, candidate.right

    if left.tax_ids and right.tax_ids and not (left.tax_ids & right.tax_ids):
        if not (left.pans & right.pans):
            objections.append(
                f"CONTRADICTORY_IDENTIFIER: different tax registrations "
                f"({sorted(left.tax_ids)[0]} vs {sorted(right.tax_ids)[0]}) with no "
                f"shared PAN — these are different legal entities"
            )

    if left.domains and right.domains and not (left.domains & right.domains):
        objections.append(
            f"CONTRADICTORY_IDENTIFIER: different corporate domains "
            f"({sorted(left.domains)[0]} vs {sorted(right.domains)[0]})"
        )

    # Shared premises without any identifier overlap is the classic false merge.
    if (
        left.addresses & right.addresses
        and not (left.tax_ids & right.tax_ids)
        and not (left.domains & right.domains)
        and not (left.tokens & right.tokens)
    ):
        objections.append(
            "SHARED_ADDRESS_NOT_SAME: identical registered address but no shared "
            "identifier or name token — shared premises, not shared identity"
        )

    return objections


async def criticise(
    candidate: MatchCandidate, *, workspace_id: str, run_id: str | None = None
) -> tuple[str, CriticVerdictModel]:
    """Challenge one proposed link. Returns `(final_decision, verdict)`.

    The final decision may only ever be the same as, or weaker than, the proposal.
    """
    proposed = candidate.decision

    # 1. Deterministic objections first — no model call needed.
    objections = deterministic_objections(candidate)
    if objections and proposed == "ACCEPTED" and candidate.exact_match is None:
        emit(
            EventKind.RULE,
            f"Match Critic: deterministic objection to "
            f"{candidate.left.display_name} ↔ {candidate.right.display_name}",
            workspace_id=workspace_id,
            severity=Severity.WARNING,
            feature=2,
            run_id=run_id,
            objections=objections,
        )
        return "REVIEW", CriticVerdictModel(
            verdict="DISPUTE",
            issue_codes=["CONTRADICTORY_IDENTIFIER"],
            reasoning=" ; ".join(objections),
        )

    # 2. An exact identifier match needs no LLM opinion. A shared PAN is a fact.
    if candidate.exact_match is not None:
        return proposed, CriticVerdictModel(
            verdict="AGREE",
            issue_codes=["NONE"],
            reasoning=(
                f"Deterministic identifier match ({candidate.exact_match.rule_id}): "
                f"{candidate.exact_match.reason}"
            ),
        )

    # 3. Only genuinely uncertain links are worth a model call.
    if proposed == "REJECTED":
        return proposed, CriticVerdictModel(
            verdict="AGREE",
            issue_codes=["INSUFFICIENT_EVIDENCE"],
            reasoning=(
                f"Match weight {candidate.total_weight:+.1f} is below the review "
                f"threshold; no merge proposed."
            ),
        )

    if not is_material(candidate):
        # Two categories are excluded here, for different reasons.
        #
        # A pair already heading to REVIEW is going to a human, who is a better
        # critic than the model; spending a call to re-route it to the same place
        # changes nothing.
        #
        # An ACCEPTED pair with overwhelming, unconflicted evidence — identical
        # names and no contradictory identifier — is not a judgement call. Asking
        # the model anyway caused real damage in testing: twelve bank narrations
        # reading "BLUE HARBOR M1" … "M12" were split into twelve separate
        # customers because a cautious NEEDS_REVIEW downgraded every link between
        # them. Over-splitting is not the safe direction it looks like; it inflates
        # the customer count and understates concentration just as a false merge does.
        return proposed, CriticVerdictModel(
            verdict="AGREE",
            issue_codes=["NONE"],
            reasoning=(
                f"Not routed to the critic: {'already queued for human review'
                if proposed == 'REVIEW'
                else f'unconflicted evidence at weight {candidate.total_weight:+.1f}'}."
            ),
        )

    if not llm.is_available():
        # Without a model the system must fail safe, not fail open: an uncertain
        # link stays uncertain rather than being accepted unchallenged.
        downgraded = "REVIEW" if proposed == "ACCEPTED" else proposed
        return downgraded, CriticVerdictModel(
            verdict="NEEDS_REVIEW",
            issue_codes=["INSUFFICIENT_EVIDENCE"],
            reasoning=(
                "No critic model configured; the proposed link was not independently "
                "challenged and is routed to human review."
            ),
        )

    try:
        result = await llm.structured_call(
            role=llm.Role.CRITIC,
            system_prompt=CRITIC_SYSTEM_PROMPT,
            user_prompt=_describe(candidate),
            schema=CriticVerdictModel,
            workspace_id=workspace_id,
            feature=2,
            run_id=run_id,
            # Measured, not guessed: gpt-oss-120b needs ~410 completion tokens
            # for this verdict because it emits reasoning before the JSON. At 300
            # every call returned HTTP 400 json_validate_failed.
            max_completion_tokens=800,
        )
        verdict: CriticVerdictModel = result.parsed
    except (llm.LLMUnavailableError, llm.LLMSchemaError) as exc:
        emit(
            EventKind.ERROR,
            f"Match Critic unavailable: {exc}",
            workspace_id=workspace_id,
            severity=Severity.WARNING,
            feature=2,
            run_id=run_id,
        )
        return ("REVIEW" if proposed == "ACCEPTED" else proposed), CriticVerdictModel(
            verdict="NEEDS_REVIEW",
            issue_codes=["INSUFFICIENT_EVIDENCE"],
            reasoning=f"Critic call failed ({type(exc).__name__}); routed to review.",
        )

    # 4. Apply the verdict. The critic may weaken a link, never strengthen it.
    if verdict.verdict == "DISPUTE":
        final = "REVIEW" if proposed == "ACCEPTED" else "REJECTED"
    elif verdict.verdict == "NEEDS_REVIEW":
        final = "REVIEW"
    else:
        final = proposed  # AGREE preserves the proposal; it cannot promote it

    emit(
        EventKind.AGENT_STEP,
        f"Match Critic: {verdict.verdict} on "
        f"{candidate.left.display_name} ↔ {candidate.right.display_name} "
        f"→ {final}",
        workspace_id=workspace_id,
        severity=Severity.SUCCESS if verdict.verdict == "AGREE" else Severity.WARNING,
        feature=2,
        run_id=run_id,
        issue_codes=verdict.issue_codes,
        reasoning=verdict.reasoning[:200],
    )
    return final, verdict
