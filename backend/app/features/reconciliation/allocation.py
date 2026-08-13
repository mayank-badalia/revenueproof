"""Constrained allocation across invoices and payments — F4 sub-feature 2.

The problem is not "which payment matches which invoice". Real books contain one
payment settling four invoices, one invoice settled by three instalments, and
payments that partly cover several bills at once. Greedy pairing gets these wrong in
a way that is invisible in the total: it will happily apply the same ₹5,00,000 twice
and report ₹10,00,000 of verified revenue.

So allocation is modelled as a constrained optimisation over **integer minor units**,
solved with OR-Tools CP-SAT (the Rank #1 choice in core_resoruces.md, "because CP-SAT
can represent subsets, split payments and many-to-many combinations directly").

Hard constraints — the solver may not violate these at any cost:
  * no invoice receives more than its outstanding balance
  * no payment is applied for more than it is worth, net of refunds
  * an allocation exists only where a candidate link was generated

Objective — among all feasible allocations, prefer the one that:
  * applies the most cash (unapplied money is an unanswered question)
  * uses higher-confidence links
  * fully settles invoices rather than leaving many part-paid

Integers are essential. In floating point "allocated == available" is not reliably
decidable, and the conservation invariant this whole feature rests on becomes
untestable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ortools.sat.python import cp_model

from app.core.events import EventKind, Severity, emit
from app.core.money import ConservationError, Money, assert_conservation

# CP-SAT is exact but exponential in the worst case. A wall-clock cap keeps a
# pathological workspace from hanging a request; the solver returns its best
# feasible answer so far, which is still constraint-satisfying.
SOLVER_TIME_LIMIT_SECONDS = 20.0
# Above this many variables the model is split into independent sub-problems by
# customer, which is exact here because allocations never cross customers.
MAX_VARIABLES_PER_SOLVE = 4000


@dataclass
class AllocationInput:
    """One side of the allocation problem, in integer minor units."""

    id: str
    amount_minor: int
    currency: str
    customer_id: str | None = None
    label: str = ""


@dataclass
class AllocationEdge:
    """A permitted allocation, with the confidence that suggested it."""

    invoice_id: str
    payment_id: str
    confidence: float
    method: str
    reasons: list[str] = field(default_factory=list)


@dataclass
class AllocationResult:
    allocations: list[dict[str, Any]] = field(default_factory=list)
    unapplied_by_payment: dict[str, int] = field(default_factory=dict)
    outstanding_by_invoice: dict[str, int] = field(default_factory=dict)
    status: str = "unknown"
    solve_seconds: float = 0.0
    conservation_ok: bool = True
    conservation_error: str | None = None

    @property
    def total_allocated_minor(self) -> int:
        return sum(a["amount_minor"] for a in self.allocations)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "allocations": len(self.allocations),
            "total_allocated_minor": self.total_allocated_minor,
            "unapplied_payments": sum(1 for v in self.unapplied_by_payment.values() if v > 0),
            "unapplied_minor": sum(self.unapplied_by_payment.values()),
            "invoices_outstanding": sum(
                1 for v in self.outstanding_by_invoice.values() if v > 0
            ),
            "outstanding_minor": sum(self.outstanding_by_invoice.values()),
            "solve_seconds": round(self.solve_seconds, 3),
            "conservation_ok": self.conservation_ok,
            "conservation_error": self.conservation_error,
        }


def allocate(
    invoices: list[AllocationInput],
    payments: list[AllocationInput],
    edges: list[AllocationEdge],
    *,
    workspace_id: str = "_system",
    time_limit: float = SOLVER_TIME_LIMIT_SECONDS,
) -> AllocationResult:
    """Solve the allocation problem exactly, or report why it could not."""
    result = AllocationResult()
    invoice_by_id = {item.id: item for item in invoices}
    payment_by_id = {item.id: item for item in payments}

    usable = [
        edge
        for edge in edges
        if edge.invoice_id in invoice_by_id and edge.payment_id in payment_by_id
    ]
    if not usable:
        result.status = "NO_CANDIDATES"
        result.unapplied_by_payment = {p.id: p.amount_minor for p in payments}
        result.outstanding_by_invoice = {i.id: i.amount_minor for i in invoices}
        return result

    # Independent sub-problems: an allocation never spans two customers, so solving
    # per customer is exact and keeps each model small.
    groups = _partition(usable, invoice_by_id, payment_by_id)
    statuses: list[str] = []

    for group_edges, group_invoices, group_payments in groups:
        sub = _solve_group(
            group_invoices, group_payments, group_edges,
            workspace_id=workspace_id, time_limit=time_limit,
        )
        result.allocations.extend(sub.allocations)
        result.unapplied_by_payment.update(sub.unapplied_by_payment)
        result.outstanding_by_invoice.update(sub.outstanding_by_invoice)
        statuses.append(sub.status)
        result.solve_seconds += sub.solve_seconds

    # Payments and invoices with no candidate at all are still accounted for:
    # silently dropping them is how unapplied cash disappears from a report.
    for payment in payments:
        result.unapplied_by_payment.setdefault(payment.id, payment.amount_minor)
    for invoice in invoices:
        result.outstanding_by_invoice.setdefault(invoice.id, invoice.amount_minor)

    if any(s in {"TOO_LARGE", "INFEASIBLE", "TIMEOUT"} for s in statuses):
        # Surface the worst outcome. Reporting "FEASIBLE" when a component was
        # skipped entirely makes an empty allocation look like a clean result.
        result.status = next(
            s for s in statuses if s in {"TOO_LARGE", "INFEASIBLE", "TIMEOUT"}
        )
    elif all(s == "OPTIMAL" for s in statuses):
        result.status = "OPTIMAL"
    else:
        result.status = "FEASIBLE"
    _verify_conservation(result, invoices, payments)
    return result


def _partition(
    edges: list[AllocationEdge],
    invoice_by_id: dict[str, AllocationInput],
    payment_by_id: dict[str, AllocationInput],
) -> list[tuple[list[AllocationEdge], list[AllocationInput], list[AllocationInput]]]:
    """Split into connected components so each solve stays small and exact."""
    parent: dict[str, str] = {}

    def find(node: str) -> str:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for edge in edges:
        union(f"i:{edge.invoice_id}", f"p:{edge.payment_id}")

    components: dict[str, list[AllocationEdge]] = {}
    for edge in edges:
        components.setdefault(find(f"i:{edge.invoice_id}"), []).append(edge)

    groups = []
    for group_edges in components.values():
        invoice_ids = {e.invoice_id for e in group_edges}
        payment_ids = {e.payment_id for e in group_edges}
        groups.append(
            (
                group_edges,
                [invoice_by_id[i] for i in sorted(invoice_ids)],
                [payment_by_id[p] for p in sorted(payment_ids)],
            )
        )
    return groups


def _solve_group(
    invoices: list[AllocationInput],
    payments: list[AllocationInput],
    edges: list[AllocationEdge],
    *,
    workspace_id: str,
    time_limit: float,
) -> AllocationResult:
    """Solve one connected component."""
    result = AllocationResult()
    model = cp_model.CpModel()

    invoice_by_id = {i.id: i for i in invoices}
    payment_by_id = {p.id: p for p in payments}

    if len(edges) > MAX_VARIABLES_PER_SOLVE:
        # Refuse rather than hang. A component this dense means the candidate
        # generator is too permissive, and a silent timeout would look like "no
        # allocations found" — indistinguishable from genuinely unmatched cash.
        emit(
            EventKind.ERROR,
            f"Allocation component has {len(edges)} candidate links, above the "
            f"{MAX_VARIABLES_PER_SOLVE} limit; skipped and routed to review",
            workspace_id=workspace_id,
            feature=4,
            severity=Severity.WARNING,
        )
        result.status = "TOO_LARGE"
        result.unapplied_by_payment = {p.id: p.amount_minor for p in payments}
        result.outstanding_by_invoice = {i.id: i.amount_minor for i in invoices}
        return result

    # One integer variable per permitted link: how much of that payment is applied
    # to that invoice, in minor units.
    amount_vars: dict[tuple[str, str], cp_model.IntVar] = {}
    used_vars: dict[tuple[str, str], cp_model.IntVar] = {}
    for edge in edges:
        invoice = invoice_by_id[edge.invoice_id]
        payment = payment_by_id[edge.payment_id]
        upper = min(invoice.amount_minor, payment.amount_minor)
        if upper <= 0:
            continue
        key = (edge.invoice_id, edge.payment_id)
        amount_vars[key] = model.NewIntVar(0, upper, f"amt_{key[0][:8]}_{key[1][:8]}")
        used_vars[key] = model.NewBoolVar(f"use_{key[0][:8]}_{key[1][:8]}")
        # Link the two: a zero allocation is an unused link.
        model.Add(amount_vars[key] > 0).OnlyEnforceIf(used_vars[key])
        model.Add(amount_vars[key] == 0).OnlyEnforceIf(used_vars[key].Not())

    if not amount_vars:
        result.status = "NO_CANDIDATES"
        result.unapplied_by_payment = {p.id: p.amount_minor for p in payments}
        result.outstanding_by_invoice = {i.id: i.amount_minor for i in invoices}
        return result

    # HARD: an invoice cannot receive more than it is owed.
    for invoice in invoices:
        applied = [v for (i, _), v in amount_vars.items() if i == invoice.id]
        if applied:
            model.Add(sum(applied) <= invoice.amount_minor)

    # HARD: a payment cannot be applied for more than it is worth. This is the
    # constraint that makes double-counting structurally impossible rather than
    # something a later check has to catch (spec §14).
    for payment in payments:
        applied = [v for (_, p), v in amount_vars.items() if p == payment.id]
        if applied:
            model.Add(sum(applied) <= payment.amount_minor)

    # Objective. Applying cash dominates; link confidence and clean settlement
    # break ties among equally-cash-efficient solutions.
    #
    # Magnitudes matter enormously here. Amounts are in minor units, so a single
    # invoice is already ~4e8; multiplying by a weight pushed objective terms past
    # 1e10 and, summed over ~1000 variables, left CP-SAT unable to make progress
    # inside the time limit — it returned a feasible-but-empty allocation, which
    # reads identically to "no payments matched". The amount enters the objective
    # unscaled, and the tie-breakers are sized relative to the largest amount in
    # this component so they can never outweigh applying real cash.
    largest = max(
        [i.amount_minor for i in invoices] + [p.amount_minor for p in payments] + [1]
    )
    # A tie-breaker worth ~0.1% of the biggest single amount: enough to order
    # equivalent solutions, never enough to justify leaving cash unapplied.
    tie_break = max(1, largest // 1000)

    # Every link carries a cost, and a higher-confidence link costs less. Rewarding
    # a link outright — as this once did — makes fragmentation *profitable*: two
    # links each earned a confidence bonus, so splitting one ₹75,000 payment across
    # two identical invoices outscored settling either cleanly. Measured on live
    # data, that produced an invoice recognised at ₹0.03 beside one at ₹74,999.94.
    # Conservation still held, so nothing failed — the figures were simply
    # indefensible, which is worse.
    #
    # Cost is bounded well below `tie_break` so it can never outweigh applying cash;
    # it only chooses between allocations that move the same rupees.
    link_cost = max(1, tie_break // 4)

    terms = []
    for (invoice_id, payment_id), var in amount_vars.items():
        edge = next(
            e for e in edges if e.invoice_id == invoice_id and e.payment_id == payment_id
        )
        terms.append(var)
        terms.append(
            used_vars[(invoice_id, payment_id)]
            * (int(edge.confidence * tie_break) - tie_break - link_cost)
        )

    # Reward fully settling an invoice, so the solver prefers one clean settlement
    # over spreading cash thinly across many part-paid bills.
    for invoice in invoices:
        applied = [v for (i, _), v in amount_vars.items() if i == invoice.id]
        if not applied:
            continue
        fully_paid = model.NewBoolVar(f"full_{invoice.id[:8]}")
        model.Add(sum(applied) == invoice.amount_minor).OnlyEnforceIf(fully_paid)
        model.Add(sum(applied) < invoice.amount_minor).OnlyEnforceIf(fully_paid.Not())
        terms.append(fully_paid * (tie_break * 2))

    model.Maximize(sum(terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit
    # Deterministic results across runs: an audit trail that changes between
    # identical runs is not an audit trail.
    solver.parameters.num_search_workers = 8
    solver.parameters.random_seed = 42
    # Deterministic despite parallelism: without this, workers race and an
    # equally-optimal but different allocation can win between runs, which would
    # make the audit trail irreproducible.
    solver.parameters.interleave_search = True

    status = solver.Solve(model)
    result.solve_seconds = solver.WallTime()

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        # UNKNOWN means the solver ran out of time, not that the problem has no
        # solution — allocating nothing is always feasible. Reporting a timeout as
        # INFEASIBLE points the reader at the data when the cause is the model.
        result.status = "TIMEOUT" if status == cp_model.UNKNOWN else "INFEASIBLE"
        result.unapplied_by_payment = {p.id: p.amount_minor for p in payments}
        result.outstanding_by_invoice = {i.id: i.amount_minor for i in invoices}
        return result

    result.status = "OPTIMAL" if status == cp_model.OPTIMAL else "FEASIBLE"

    applied_per_payment: dict[str, int] = {p.id: 0 for p in payments}
    applied_per_invoice: dict[str, int] = {i.id: 0 for i in invoices}

    for (invoice_id, payment_id), var in amount_vars.items():
        value = solver.Value(var)
        if value <= 0:
            continue
        edge = next(
            e for e in edges if e.invoice_id == invoice_id and e.payment_id == payment_id
        )
        result.allocations.append(
            {
                "invoice_id": invoice_id,
                "payment_id": payment_id,
                "amount_minor": value,
                "currency": invoice_by_id[invoice_id].currency,
                "confidence": edge.confidence,
                "method": edge.method,
                "reasons": edge.reasons,
            }
        )
        applied_per_payment[payment_id] += value
        applied_per_invoice[invoice_id] += value

    result.unapplied_by_payment = {
        p.id: p.amount_minor - applied_per_payment[p.id] for p in payments
    }
    result.outstanding_by_invoice = {
        i.id: i.amount_minor - applied_per_invoice[i.id] for i in invoices
    }
    return result


def _verify_conservation(
    result: AllocationResult,
    invoices: list[AllocationInput],
    payments: list[AllocationInput],
) -> None:
    """Check that no value was created or destroyed.

    The solver's constraints should make this impossible, which is exactly why it is
    asserted: a silent modelling error here would understate or invent revenue, and
    an invariant that only holds when the code is correct is worth nothing.
    """
    currency = invoices[0].currency if invoices else "INR"
    try:
        for payment in payments:
            applied = sum(
                a["amount_minor"] for a in result.allocations if a["payment_id"] == payment.id
            )
            unapplied = result.unapplied_by_payment.get(payment.id, 0)
            assert_conservation(
                Money(payment.amount_minor, currency),
                Money(applied, currency),
                Money(unapplied, currency),
            )
        for invoice in invoices:
            applied = sum(
                a["amount_minor"] for a in result.allocations if a["invoice_id"] == invoice.id
            )
            outstanding = result.outstanding_by_invoice.get(invoice.id, 0)
            assert_conservation(
                Money(invoice.amount_minor, currency),
                Money(applied, currency),
                Money(outstanding, currency),
            )
    except ConservationError as exc:
        result.conservation_ok = False
        result.conservation_error = str(exc)
