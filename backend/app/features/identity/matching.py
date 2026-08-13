"""Candidate blocking and probabilistic match ranking — Feature 2, sub-features 2-3.

**Deviation from the ranked resource, stated plainly.** core_resoruces.md ranks
Splink #1 for cross-system linkage. The properties it ranks Splink *for* are
explicit blocking rules, calibrated match weights, thresholds and diagnostics —
"better than an embedding-only approach because it exposes blocking rules, match
weights, thresholds and diagnostics."

This module implements that same Fellegi–Sunter model directly: each comparison
contributes a log2 match weight, weights sum to a total, and the total maps to a
probability. What is not used is Splink's EM parameter estimation, because it needs
a large record set to converge — a RevenueProof workspace holds tens of customers,
not millions, and unsupervised EM on 20 records produces unstable weights that
would be *less* trustworthy than hand-set ones a reviewer can read and challenge.
Every weight below is therefore an explicit, auditable constant, and
`evaluation.py` measures them against labelled pairs exactly as Splink's diagnostics
would. If a workspace ever reaches a scale where EM helps, the scorer can be
swapped without changing anything upstream or downstream.

Tier discipline (idea_features.md §6.3): exact identifiers decide; fuzzy names rank;
semantic similarity only ever *supports* — it can never carry a pair over the
acceptance threshold on its own.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from app.features.identity.identifiers import (
    ExactMatch,
    IdentityKeys,
    find_exact_match,
)

# --- Match weights, in log2 odds ------------------------------------------
#
# A weight of +N means the evidence makes a true match 2^N times more likely.
# Negative weights are disagreements. These are set from how much each identifier
# actually constrains identity, not fitted — and are validated in evaluation.py.

W_NAME_EXACT = 8.0            # identical normalised names
W_NAME_VERY_HIGH = 5.5        # ≥95 similarity
W_NAME_HIGH = 3.5             # ≥88
W_NAME_MODERATE = 1.5         # ≥80
W_NAME_LOW = -1.0             # ≥65 — weak, slightly against
W_NAME_MISMATCH = -6.0        # below 65: strong evidence these differ

W_TOKEN_ALL_SHARED = 3.0      # every distinctive token in common
W_TOKEN_SOME_SHARED = 1.2
W_TOKEN_NONE_SHARED = -3.5

W_DOMAIN_MATCH = 4.5
W_DOMAIN_CONFLICT = -4.0      # both have corporate domains and they differ
W_PHONE_MATCH = 3.0
W_ADDRESS_MATCH = 2.0         # shared premises: suggestive, not identifying
W_POSTAL_MATCH = 0.5
W_TAX_CONFLICT = -9.0         # different GSTINs ⇒ different registrations
# Deliberately modest: enough to lift an abbreviation pair into REVIEW, never
# enough to reach ACCEPTED on its own.
W_ABBREVIATION = 4.0

# Semantic similarity is capped deliberately. core_resoruces.md: embeddings are a
# "tertiary feature" and must never override verified identifiers.
W_SEMANTIC_MAX = 1.0

# Log2 odds thresholds. 6.0 ≈ 98.4% posterior at even priors.
THRESHOLD_ACCEPT = 6.0
THRESHOLD_REVIEW = 1.5


@dataclass
class Comparison:
    """One evidence contribution, kept for the explanation shown to a reviewer."""

    field: str
    outcome: str
    weight: float
    detail: str = ""


@dataclass
class MatchCandidate:
    """A scored pair, with the full evidence trail behind the score."""

    left: IdentityKeys
    right: IdentityKeys
    total_weight: float = 0.0
    comparisons: list[Comparison] = field(default_factory=list)
    exact_match: ExactMatch | None = None
    blocking_rule: str = ""
    semantic_similarity: float | None = None

    @property
    def probability(self) -> float:
        """Convert summed log2 odds to a posterior probability at even priors."""
        # Clamped to avoid overflow on extreme weights.
        odds = 2.0 ** max(-60.0, min(60.0, self.total_weight))
        return odds / (1.0 + odds)

    @property
    def decision(self) -> str:
        """ACCEPTED / REVIEW / REJECTED, before the critic weighs in."""
        if self.exact_match is not None:
            return "ACCEPTED"
        if self.total_weight >= THRESHOLD_ACCEPT:
            return "ACCEPTED"
        if self.total_weight >= THRESHOLD_REVIEW:
            return "REVIEW"
        return "REJECTED"

    @property
    def method(self) -> str:
        if self.exact_match is not None:
            return "exact"
        if self.semantic_similarity is not None:
            return "semantic"
        return "fuzzy"

    def explain(self) -> list[dict]:
        """Serialisable evidence, ordered by how much each item mattered."""
        return [
            {
                "field": c.field,
                "outcome": c.outcome,
                "weight": round(c.weight, 2),
                "detail": c.detail,
            }
            for c in sorted(self.comparisons, key=lambda c: -abs(c.weight))
        ]


# ---------------------------------------------------------------------------
# Blocking — generate plausible pairs without comparing everything to everything
# ---------------------------------------------------------------------------


def build_blocks(records: list[IdentityKeys]) -> dict[str, list[str]]:
    """Group records by cheap keys that a true match would almost certainly share.

    Comparing every record to every other is O(n²); at a few hundred source records
    per workspace that is survivable, but blocking also *documents* why a pair was
    never considered, which matters when a reviewer asks why two records were not
    linked. Recall of these rules is measured in `evaluation.py`.
    """
    blocks: dict[str, list[str]] = defaultdict(list)
    for record in records:
        for pan in record.pans:
            blocks[f"pan:{pan}"].append(record.record_id)
        for tax_id in record.tax_ids:
            blocks[f"tax:{tax_id}"].append(record.record_id)
        for domain in record.domains:
            blocks[f"domain:{domain}"].append(record.record_id)
        for email in record.emails:
            blocks[f"email:{email}"].append(record.record_id)
        for phone in record.phones:
            blocks[f"phone:{phone}"].append(record.record_id)
        for code in record.postal_codes:
            blocks[f"pin:{code}"].append(record.record_id)
        # Name-based keys catch the case where no structured identifier exists,
        # which is normal for bank narrations and payment descriptions.
        for token in record.tokens:
            if len(token) >= 4:
                blocks[f"token:{token}"].append(record.record_id)
        if record.normalized_name:
            blocks[f"prefix:{record.normalized_name[:5]}"].append(record.record_id)
            # Character n-grams recover abbreviations and truncations that token
            # blocking misses entirely. A bank narration "NSTAR TECH PVT" shares no
            # whole token with "Northstar Technologies", but both contain "star".
            # Without this the pair is never even considered — a silent recall hole
            # precisely where identifiers are weakest.
            for gram in _character_ngrams(record.normalized_name):
                blocks[f"ngram:{gram}"].append(record.record_id)
    return dict(blocks)


_NGRAM_SIZE = 4


def _character_ngrams(name: str) -> set[str]:
    """Distinct n-grams of a normalised name, ignoring spaces."""
    compact = name.replace(" ", "")
    if len(compact) < _NGRAM_SIZE:
        return {compact} if compact else set()
    return {
        compact[i : i + _NGRAM_SIZE]
        for i in range(len(compact) - _NGRAM_SIZE + 1)
    }


def generate_candidate_pairs(
    records: list[IdentityKeys], *, max_block_size: int = 60
) -> list[tuple[str, str, str]]:
    """Return `(left_id, right_id, blocking_rule)` for every plausible pair.

    Oversized blocks are skipped: a block containing 500 records produces 125,000
    pairs and is almost always a generic token rather than a real signal. The skip
    is reported so it cannot silently reduce recall.
    """
    blocks = build_blocks(records)
    seen: set[tuple[str, str]] = set()
    pairs: list[tuple[str, str, str]] = []

    for block_key, members in blocks.items():
        unique = sorted(set(members))
        if len(unique) < 2 or len(unique) > max_block_size:
            continue
        for index, left in enumerate(unique):
            for right in unique[index + 1 :]:
                key = (left, right)
                if key in seen:
                    continue
                seen.add(key)
                pairs.append((left, right, block_key))
    return pairs


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def name_similarity(left: IdentityKeys, right: IdentityKeys) -> float:
    """0–100 similarity over normalised names.

    `token_sort_ratio` handles reordering ("Analytics Blue Harbor"); plain ratio
    handles truncation ("Northstar Tech" vs "Northstar Technologies"). The stronger
    of the two is used so neither failure mode alone sinks a real match.
    """
    a, b = left.normalized_name, right.normalized_name
    if not a or not b:
        return 0.0
    return max(fuzz.ratio(a, b), fuzz.token_sort_ratio(a, b))


def _containment_score(
    left: IdentityKeys, right: IdentityKeys
) -> tuple[float, str, str] | None:
    """Detect one name being an abbreviated form of the other.

    Requires a materially shorter name and a high partial match, so unrelated
    companies sharing a common substring do not trigger it.
    """
    a, b = left.normalized_name.replace(" ", ""), right.normalized_name.replace(" ", "")
    if not a or not b:
        return None
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) < 4 or len(shorter) >= len(longer):
        return None
    # The shorter name must be a near-substring of the longer one.
    score = fuzz.partial_ratio(shorter, longer)
    if score < 88:
        return None
    short_display = (
        left.display_name if a is shorter else right.display_name
    )
    long_display = right.display_name if a is shorter else left.display_name
    return score, short_display, long_display


def score_pair(
    left: IdentityKeys,
    right: IdentityKeys,
    *,
    blocking_rule: str = "",
    semantic_similarity: float | None = None,
) -> MatchCandidate:
    """Score one candidate pair, recording every contribution."""
    candidate = MatchCandidate(
        left=left, right=right, blocking_rule=blocking_rule,
        semantic_similarity=semantic_similarity,
    )

    # Tier 1 — a deterministic hit short-circuits everything below it.
    exact = find_exact_match(left, right)
    if exact is not None:
        candidate.exact_match = exact
        candidate.total_weight = 20.0  # effectively certain
        candidate.comparisons.append(
            Comparison("exact_identifier", exact.rule_id, 20.0, exact.reason)
        )
        return candidate

    # Tier 2 — name similarity.
    similarity = name_similarity(left, right)
    if similarity >= 99.5:
        weight, outcome = W_NAME_EXACT, "identical"
    elif similarity >= 95:
        weight, outcome = W_NAME_VERY_HIGH, "very_high"
    elif similarity >= 88:
        weight, outcome = W_NAME_HIGH, "high"
    elif similarity >= 80:
        weight, outcome = W_NAME_MODERATE, "moderate"
    elif similarity >= 65:
        weight, outcome = W_NAME_LOW, "low"
    else:
        weight, outcome = W_NAME_MISMATCH, "mismatch"
    candidate.comparisons.append(
        Comparison("name", outcome, weight,
                   f"'{left.display_name}' vs '{right.display_name}' "
                   f"(similarity {similarity:.0f})")
    )

    # Distinctive-token overlap, independent of string edit distance.
    if left.tokens and right.tokens:
        shared = left.tokens & right.tokens
        smaller = min(len(left.tokens), len(right.tokens))
        if shared and len(shared) == smaller:
            candidate.comparisons.append(
                Comparison("tokens", "all_shared", W_TOKEN_ALL_SHARED,
                           f"shared: {', '.join(sorted(shared))}")
            )
        elif shared:
            candidate.comparisons.append(
                Comparison("tokens", "some_shared", W_TOKEN_SOME_SHARED,
                           f"shared: {', '.join(sorted(shared))}")
            )
        else:
            candidate.comparisons.append(
                Comparison("tokens", "none_shared", W_TOKEN_NONE_SHARED,
                           "no distinctive tokens in common")
            )

    # Domain agreement or conflict.
    if left.domains and right.domains:
        shared_domains = left.domains & right.domains
        if shared_domains:
            candidate.comparisons.append(
                Comparison("domain", "match", W_DOMAIN_MATCH,
                           f"shared domain {sorted(shared_domains)[0]}")
            )
        else:
            candidate.comparisons.append(
                Comparison("domain", "conflict", W_DOMAIN_CONFLICT,
                           f"{sorted(left.domains)[0]} vs {sorted(right.domains)[0]}")
            )

    # Conflicting tax IDs are near-decisive evidence *against* a merge — this is
    # what keeps Blue Harbor Analytics and Blue Harbour Logistics apart.
    if left.tax_ids and right.tax_ids and not (left.tax_ids & right.tax_ids):
        candidate.comparisons.append(
            Comparison("tax_id", "conflict", W_TAX_CONFLICT,
                       f"{sorted(left.tax_ids)[0]} vs {sorted(right.tax_ids)[0]} — "
                       f"different registrations")
        )

    # Abbreviation / containment. Bank narrations truncate aggressively
    # ("NSTAR TECH PVT" for "Northstar Technologies"), so a shorter name contained
    # within a longer one is real evidence that plain edit distance misses.
    # Weighted to reach REVIEW rather than ACCEPTED on its own: a narration should
    # be confirmed by Feature 4's amount/date/reference reconciliation, not by name.
    containment = _containment_score(left, right)
    if containment is not None:
        score, shorter, longer = containment
        candidate.comparisons.append(
            Comparison(
                "abbreviation", "contained", W_ABBREVIATION,
                f"'{shorter}' appears within '{longer}' "
                f"(partial similarity {score:.0f}) — typical of a bank narration",
            )
        )

    if left.phones & right.phones:
        candidate.comparisons.append(
            Comparison("phone", "match", W_PHONE_MATCH,
                       f"shared number ending {sorted(left.phones & right.phones)[0][-4:]}")
        )

    if left.addresses & right.addresses:
        candidate.comparisons.append(
            Comparison("address", "match", W_ADDRESS_MATCH,
                       "identical registered address — shared premises, "
                       "not proof of the same customer")
        )
    elif left.postal_codes & right.postal_codes:
        candidate.comparisons.append(
            Comparison("postal_code", "match", W_POSTAL_MATCH, "same PIN code")
        )

    # Tier 3 — semantic similarity, capped so it can never decide a match alone.
    if semantic_similarity is not None and semantic_similarity > 0.5:
        weight = min(W_SEMANTIC_MAX, (semantic_similarity - 0.5) * 2 * W_SEMANTIC_MAX)
        candidate.comparisons.append(
            Comparison("semantic", "supporting", weight,
                       f"embedding similarity {semantic_similarity:.2f} "
                       f"(supporting evidence only)")
        )

    candidate.total_weight = sum(c.weight for c in candidate.comparisons)
    return candidate


def rank_candidates(
    records: list[IdentityKeys],
    *,
    semantic_scores: dict[tuple[str, str], float] | None = None,
    max_block_size: int = 60,
) -> list[MatchCandidate]:
    """Block, score and rank every plausible pair, best first."""
    by_id = {record.record_id: record for record in records}
    semantic_scores = semantic_scores or {}

    scored: list[MatchCandidate] = []
    for left_id, right_id, rule in generate_candidate_pairs(
        records, max_block_size=max_block_size
    ):
        left, right = by_id.get(left_id), by_id.get(right_id)
        if left is None or right is None:
            continue
        semantic = semantic_scores.get((left_id, right_id)) or semantic_scores.get(
            (right_id, left_id)
        )
        scored.append(
            score_pair(left, right, blocking_rule=rule, semantic_similarity=semantic)
        )

    return sorted(scored, key=lambda c: -c.total_weight)


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------


def cannot_link_pairs(candidates: list[MatchCandidate]) -> set[tuple[str, str]]:
    """Pairs that carry a positive contradiction and must never share a cluster.

    A conflicting tax registration or corporate domain is evidence these are
    different legal entities. Detecting the conflict is not enough — the pair must
    be prevented from merging *transitively* through a third record that carries no
    identifiers at all.
    """
    blocked: set[tuple[str, str]] = set()
    for candidate in candidates:
        if candidate.decision == "ACCEPTED":
            continue
        if any(
            comparison.field in {"tax_id", "domain"} and comparison.outcome == "conflict"
            for comparison in candidate.comparisons
        ):
            blocked.add(
                tuple(sorted((candidate.left.record_id, candidate.right.record_id)))
            )
    return blocked


def cluster_accepted(
    candidates: list[MatchCandidate],
    records: list[IdentityKeys],
    *,
    cannot_link: set[tuple[str, str]] | None = None,
) -> tuple[dict[str, set[str]], list[dict]]:
    """Group accepted links into customer clusters, respecting cannot-link constraints.

    Plain union-find is unsafe here. Two companies with conflicting tax IDs can still
    be pulled into one cluster by a third record that matches both and carries no
    identifiers — a bank narration like "BLUE HARBOR" matches both Blue Harbor
    Analytics and Blue Harbour Logistics. Transitivity then merges two genuinely
    different customers, understating customer concentration and potentially hiding a
    related party. That is the single most damaging error this feature can make.

    So merges are *constrained*: before joining two components, if any pair across
    them is known-different, the merge is refused and reported for review. Only
    ACCEPTED pairs merge at all — idea_features.md §14 forbids an unresolved match
    from supporting verified revenue.

    Returns `(clusters, blocked_merges)`.
    """
    cannot_link = cannot_link or set()
    parent: dict[str, str] = {record.record_id: record.record_id for record in records}
    members: dict[str, set[str]] = {
        record.record_id: {record.record_id} for record in records
    }
    blocked_merges: list[dict] = []

    def find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]  # path compression
            node = parent[node]
        return node

    by_id = {record.record_id: record for record in records}

    # Strongest links first, so a well-evidenced merge is never blocked by having
    # arrived after a weaker one.
    for candidate in sorted(candidates, key=lambda c: -c.total_weight):
        if candidate.decision != "ACCEPTED":
            continue
        root_a, root_b = find(candidate.left.record_id), find(candidate.right.record_id)
        if root_a == root_b:
            continue

        # Would joining these components put a known-different pair together?
        # Sorted so the reported violation is stable across runs: iterating the set
        # directly can surface a different pair each time, which produces a different
        # review-item title and makes re-runs accumulate duplicates.
        violation = next(
            (
                pair
                for pair in sorted(cannot_link)
                if (pair[0] in members[root_a] and pair[1] in members[root_b])
                or (pair[1] in members[root_a] and pair[0] in members[root_b])
            ),
            None,
        )
        if violation is not None:
            left_name = by_id[violation[0]].display_name if violation[0] in by_id else violation[0]
            right_name = by_id[violation[1]].display_name if violation[1] in by_id else violation[1]
            blocked_merges.append(
                {
                    "via_left": candidate.left.display_name,
                    "via_right": candidate.right.display_name,
                    "would_have_merged": [left_name, right_name],
                    "weight": round(candidate.total_weight, 2),
                    "reason": (
                        f"merging these would place '{left_name}' and '{right_name}' in "
                        f"one customer, but those two carry contradictory identifiers"
                    ),
                    "evidence": candidate.explain(),
                }
            )
            continue

        # Deterministic ordering so reruns produce identical cluster IDs.
        winner, loser = (root_a, root_b) if root_a < root_b else (root_b, root_a)
        parent[loser] = winner
        members[winner] |= members[loser]
        members[loser] = set()

    clusters: dict[str, set[str]] = defaultdict(set)
    for record in records:
        clusters[find(record.record_id)].add(record.record_id)
    return dict(clusters), blocked_merges


def transitivity_conflicts(
    candidates: list[MatchCandidate], clusters: dict[str, set[str]]
) -> list[dict]:
    """Find clusters that merged records an explicit rule said were different.

    A merges with B, B merges with C, but A-vs-C scored as a rejection. Union-find
    will silently combine all three. That is precisely how a false merge happens,
    so every instance is surfaced for review rather than accepted.
    """
    member_of = {
        member: root for root, members in clusters.items() for member in members
    }
    conflicts: list[dict] = []
    for candidate in candidates:
        if candidate.decision != "REJECTED":
            continue
        # Only a genuine *contradiction* counts. Two records can legitimately share
        # a customer while looking unalike directly — a contract naming the legal
        # entity and a CRM row named after the website will not resemble each other,
        # yet an invoice carrying both identifiers rightly links them. Absence of
        # evidence is not evidence of absence, and treating it as a conflict would
        # bury reviewers in noise. A conflicting tax ID or domain is different: that
        # is positive evidence these are separate registrations.
        if not any(
            comparison.outcome == "conflict" for comparison in candidate.comparisons
        ):
            continue
        left_root = member_of.get(candidate.left.record_id)
        right_root = member_of.get(candidate.right.record_id)
        if left_root is not None and left_root == right_root:
            conflicts.append(
                {
                    "cluster": left_root,
                    "left": candidate.left.display_name,
                    "right": candidate.right.display_name,
                    "weight": round(candidate.total_weight, 2),
                    "reason": (
                        "merged transitively through another record, but compared "
                        "directly they carry contradictory identifiers"
                    ),
                    "evidence": candidate.explain(),
                }
            )
    return conflicts


def entropy_of_cluster_sizes(clusters: dict[str, set[str]]) -> float:
    """Diagnostic: low entropy means everything collapsed into one blob."""
    sizes = [len(members) for members in clusters.values()]
    total = sum(sizes)
    if total == 0:
        return 0.0
    return -sum((s / total) * math.log2(s / total) for s in sizes if s)
