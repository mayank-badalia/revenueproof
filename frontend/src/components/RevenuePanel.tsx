"use client";

/**
 * Feature 5 UI — the headline the whole product exists for.
 *
 * The claim sits beside what the evidence supports, and every rupee of the
 * difference is named. That framing is the point: a reviewer should never see a
 * single "revenue" figure, because the interesting question is always *which part
 * of the claim is proven and which part is not*.
 *
 * Three deliberate choices:
 *
 * * **ARR is shown separately from revenue.** They are different claims and get
 *   overstated in different ways — one-time fees inflate ARR without touching
 *   revenue at all.
 * * **Every deduction in the waterfall carries a reason.** "Refunded or reversed:
 *   ₹16,52,000" with the note "money received and later returned" is an argument;
 *   the number alone is just an adjustment.
 * * **The policy caveat is always visible.** These figures come from RevenueProof's
 *   stated policy, not an accounting standard, and hiding that would misrepresent
 *   what the product can tell you.
 */

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { ClassifiedItem, RevenueClassName, RevenueRun } from "@/lib/types";

const CLASS_LABEL: Record<RevenueClassName, string> = {
  VERIFIED_RECURRING: "Verified recurring",
  VERIFIED_ONE_TIME: "Verified one-time",
  CONTRACTED_UNPAID: "Contracted, unbilled",
  INVOICED_UNPAID: "Invoiced, unpaid",
  REFUNDED_OR_REVERSED: "Refunded / reversed",
  PAYMENT_WITHOUT_SUPPORT: "Cash without support",
  UNSUPPORTED_CLAIM: "Unsupported",
  HUMAN_REVIEW: "Needs review",
};

const CLASS_STYLE: Record<RevenueClassName, string> = {
  VERIFIED_RECURRING: "bg-emerald-100 text-emerald-900",
  VERIFIED_ONE_TIME: "bg-teal-100 text-teal-900",
  CONTRACTED_UNPAID: "bg-sky-100 text-sky-900",
  INVOICED_UNPAID: "bg-amber-100 text-amber-900",
  REFUNDED_OR_REVERSED: "bg-rose-100 text-rose-900",
  PAYMENT_WITHOUT_SUPPORT: "bg-orange-100 text-orange-900",
  UNSUPPORTED_CLAIM: "bg-slate-200 text-slate-800",
  HUMAN_REVIEW: "bg-violet-100 text-violet-900",
};

const STRENGTH_STYLE: Record<string, string> = {
  STRONG: "text-emerald-700",
  MODERATE: "text-sky-700",
  LIMITED: "text-amber-700",
  DISPUTED: "text-rose-700",
};

export function RevenuePanel({
  workspaceId,
  refreshKey,
  onChanged,
}: {
  workspaceId: string;
  refreshKey?: number;
  onChanged?: () => void;
}) {
  const [run, setRun] = useState<RevenueRun | null>(null);
  const [items, setItems] = useState<ClassifiedItem[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [classFilter, setClassFilter] = useState<RevenueClassName | "ALL">("ALL");

  const loadItems = useCallback(async () => {
    try {
      setItems((await api.listRevenueItems(workspaceId)).items);
    } catch {
      // A missing item list is not worth an error banner; the run button is the
      // primary action and will populate it.
    }
  }, [workspaceId]);

  // Items persist in the database; the summary built from them does not. Loading
  // only the items left the page listing 62 classified rows with no statement of
  // what they came to against the claim — the one thing this panel is for. Both are
  // restored on mount, the summary recomputed server-side. It is cleared only when
  // the workspace changes, never by a sibling panel's refresh.
  useEffect(() => {
    loadItems();
  }, [loadItems, refreshKey]);

  // `refreshKey` matters as much as the workspace does. Without it the items
  // refetched after a run and the summary above them did not, so the panel showed
  // last run's total over this run's rows — three screens quoting three different
  // figures for one workspace, which is the specific thing this product cannot do.
  useEffect(() => {
    let cancelled = false;
    setRun(null);
    api
      .revenueSummary(workspaceId)
      .then((stored) => {
        if (!cancelled && stored.verified) setRun(stored);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [workspaceId, refreshKey]);

  async function verify() {
    setBusy(true);
    setError(null);
    try {
      setRun(await api.verifyRevenue(workspaceId));
      await loadItems();
      onChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Verification failed");
    } finally {
      setBusy(false);
    }
  }

  const visible =
    classFilter === "ALL"
      ? items
      : items.filter((item) => item.classification === classFilter);

  const totals = run?.totals;
  const money = run?.money;

  return (
    <section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold">Revenue truth</h2>
          <p className="mt-0.5 text-xs text-slate-600">
            Compares the claim against what contracts, invoices, payments and bank
            receipts actually support. Every item gets one of eight states and a
            named rule. Deterministic — no model calls.
          </p>
        </div>
        <button
          type="button"
          onClick={verify}
          disabled={busy}
          className="rounded-md bg-slate-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-slate-800 disabled:opacity-50"
        >
          {busy ? "Verifying…" : "Verify revenue"}
        </button>
      </div>

      {error && (
        <p role="alert" className="mt-3 rounded-md bg-rose-50 px-3 py-2 text-xs text-rose-700">
          {error}
        </p>
      )}

      {run && totals && money && (
        <>
          {/* Claim beside evidence — never a single "revenue" number. */}
          <div className="mt-4 grid gap-3 sm:grid-cols-2">
            <div className="rounded-md border border-slate-300 bg-slate-50 p-3">
              <p className="text-[11px] uppercase tracking-wide text-slate-500">
                Revenue
              </p>
              <div className="mt-1 flex items-baseline justify-between gap-2">
                <span className="text-xs text-slate-600">Claimed</span>
                <span className="text-lg font-semibold tabular-nums">
                  {money.claimed_revenue?.display}
                </span>
              </div>
              <div className="mt-1 flex items-baseline justify-between gap-2">
                <span className="text-xs text-slate-600">Evidence-supported (before review)</span>
                <span className="text-lg font-semibold tabular-nums text-emerald-700">
                  {money.total_verified?.display}
                </span>
              </div>
              {totals.unexplained_claim > 0 && (
                <p className="mt-2 rounded bg-amber-100 px-2 py-1 text-[11px] text-amber-900">
                  {money.unexplained_claim?.display} of the claim has no supporting
                  evidence.
                </p>
              )}
            </div>

            <div className="rounded-md border border-slate-300 bg-slate-50 p-3">
              <p className="text-[11px] uppercase tracking-wide text-slate-500">
                ARR
              </p>
              <div className="mt-1 flex items-baseline justify-between gap-2">
                <span className="text-xs text-slate-600">Claimed</span>
                <span className="text-lg font-semibold tabular-nums">
                  {money.claimed_arr?.display}
                </span>
              </div>
              <div className="mt-1 flex items-baseline justify-between gap-2">
                <span className="text-xs text-slate-600">Supported (recurring only)</span>
                <span className="text-lg font-semibold tabular-nums text-emerald-700">
                  {money.supported_arr?.display}
                </span>
              </div>
              {totals.arr_gap > 0 && (
                <p className="mt-2 rounded bg-amber-100 px-2 py-1 text-[11px] text-amber-900">
                  {money.arr_gap?.display} of claimed ARR is not supported by a
                  recurring contract.
                </p>
              )}
            </div>
          </div>

          {/* An ARR of zero because contracts are unread is a very different fact
              from a company with no recurring revenue. Say which one it is. */}
          {run.contracts_unread > 0 && (
            <p className="mt-3 rounded-md bg-sky-50 px-3 py-2 text-xs text-sky-900">
              <span className="font-semibold">
                {run.contracts_unread} contracts have not been read yet.
              </span>{" "}
              Nothing can be classified as recurring until their terms are extracted,
              so supported ARR reads as zero. Run <em>Read contracts</em> first for a
              complete picture.
            </p>
          )}

          {run.double_count_conflicts.length > 0 && (
            <div className="mt-3 rounded-md border border-rose-200 bg-rose-50 p-3">
              <p className="text-xs font-semibold text-rose-900">
                {run.double_count_conflicts.length} double-count conflicts
              </p>
              {run.double_count_conflicts.slice(0, 3).map((conflict) => (
                <p key={conflict.evidence_id} className="mt-1 text-[11px] text-rose-800">
                  {conflict.reason}
                </p>
              ))}
            </div>
          )}

          {/* Waterfall: claimed → deductions → verified, each with a reason. */}
          <div className="mt-5">
            <h3 className="text-xs font-semibold text-slate-700">
              Claimed to verified
            </h3>
            <ul className="mt-2 space-y-1">
              {run.waterfall.map((step) => (
                <li
                  key={step.label}
                  className={`flex flex-wrap items-baseline justify-between gap-2 rounded border px-2 py-1.5 ${
                    step.kind === "total"
                      ? "border-emerald-300 bg-emerald-50"
                      : "border-slate-200"
                  }`}
                >
                  <span className="text-xs">
                    <span
                      className={
                        step.kind === "deduction"
                          ? "text-rose-700"
                          : step.kind === "addition"
                            ? "text-sky-700"
                            : step.kind === "total"
                              ? "font-semibold text-emerald-800"
                              : "font-medium"
                      }
                    >
                      {step.kind === "deduction"
                        ? "− "
                        : step.kind === "addition"
                          ? "+ "
                          : step.kind === "total"
                            ? "= "
                            : ""}
                      {step.label}
                    </span>
                    {step.note && (
                      <span className="ml-2 text-[11px] text-slate-500">{step.note}</span>
                    )}
                  </span>
                  <span className="text-xs font-medium tabular-nums">
                    {step.money.display}
                  </span>
                </li>
              ))}
            </ul>
          </div>

          {run.concentration.length > 0 && (
            <div className="mt-5">
              <h3 className="text-xs font-semibold text-slate-700">
                Customer concentration (verified revenue)
              </h3>
              <ul className="mt-2 space-y-1">
                {run.concentration.slice(0, 5).map((entry) => (
                  <li key={entry.customer} className="text-xs">
                    <div className="flex items-baseline justify-between gap-2">
                      <span>{entry.customer}</span>
                      <span className="tabular-nums text-slate-600">
                        {entry.share_pct.toFixed(1)}%
                      </span>
                    </div>
                    <div className="mt-0.5 h-1.5 w-full rounded bg-slate-100">
                      <div
                        className="h-1.5 rounded bg-slate-700"
                        style={{ width: `${Math.min(100, entry.share_pct)}%` }}
                      />
                    </div>
                  </li>
                ))}
              </ul>
            </div>
          )}

          <p className="mt-4 rounded-md bg-slate-50 px-3 py-2 text-[11px] text-slate-600">
            <span className="font-medium">Policy {run.policy_version}.</span>{" "}
            {run.policy.caveat}
          </p>
        </>
      )}

      {/* Per-item classification, always available from stored results. */}
      {items.length > 0 && (
        <div className="mt-5">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h3 className="text-xs font-semibold text-slate-700">
              Classified items ({items.length})
            </h3>
            <div className="flex flex-wrap gap-1">
              <button
                type="button"
                onClick={() => setClassFilter("ALL")}
                className={`rounded px-2 py-1 text-[11px] ${
                  classFilter === "ALL"
                    ? "bg-slate-900 text-white"
                    : "border border-slate-300 text-slate-600 hover:bg-slate-50"
                }`}
              >
                all
              </button>
              {(Object.keys(CLASS_LABEL) as RevenueClassName[])
                .filter((name) => run?.by_class?.[name])
                .map((name) => (
                  <button
                    key={name}
                    type="button"
                    onClick={() => setClassFilter(name)}
                    className={`rounded px-2 py-1 text-[11px] ${
                      classFilter === name
                        ? "bg-slate-900 text-white"
                        : "border border-slate-300 text-slate-600 hover:bg-slate-50"
                    }`}
                  >
                    {CLASS_LABEL[name]} ({run?.by_class?.[name]})
                  </button>
                ))}
            </div>
          </div>

          <div className="mt-2 max-h-96 overflow-auto rounded border border-slate-200">
            <table className="w-full min-w-[760px] text-left text-xs">
              <thead className="sticky top-0 bg-slate-50 text-slate-500">
                <tr>
                  <th className="px-2 py-1.5 font-medium">Item</th>
                  <th className="px-2 py-1.5 font-medium">Classification</th>
                  <th className="px-2 py-1.5 font-medium">Evidence</th>
                  <th className="px-2 py-1.5 text-right font-medium">Gross</th>
                  <th className="px-2 py-1.5 text-right font-medium">Recognised</th>
                </tr>
              </thead>
              <tbody>
                {visible.map((entry) => (
                  <tr
                    key={entry.id}
                    onClick={() =>
                      setExpanded(expanded === entry.id ? null : entry.id)
                    }
                    className="cursor-pointer border-t border-slate-100 align-top hover:bg-slate-50"
                  >
                    <td className="px-2 py-1.5">
                      <span className="font-medium">{entry.description}</span>
                      {entry.is_material && (
                        <span className="ml-1 rounded bg-slate-200 px-1 text-[10px] text-slate-700">
                          material
                        </span>
                      )}
                      {expanded === entry.id && (
                        <div className="mt-1.5 space-y-1 font-normal">
                          <p className="text-[11px] text-slate-700">
                            <span className="font-medium">{entry.rule_id}:</span>{" "}
                            {entry.rule_explanation}
                          </p>
                          {entry.missing_evidence.length > 0 && (
                            <p className="text-[11px] text-amber-800">
                              Missing: {entry.missing_evidence.join(", ")}
                            </p>
                          )}
                          {entry.evidence_ids.length > 0 && (
                            <p className="font-mono text-[10px] text-slate-500">
                              {entry.evidence_ids.join(" · ")}
                            </p>
                          )}
                        </div>
                      )}
                    </td>
                    <td className="px-2 py-1.5">
                      <span
                        className={`rounded px-1.5 py-0.5 text-[10px] ${
                          CLASS_STYLE[entry.classification]
                        }`}
                      >
                        {CLASS_LABEL[entry.classification]}
                      </span>
                    </td>
                    <td
                      className={`px-2 py-1.5 text-[11px] ${
                        STRENGTH_STYLE[entry.evidence_strength] ?? ""
                      }`}
                    >
                      {entry.evidence_strength.toLowerCase()}
                    </td>
                    <td className="px-2 py-1.5 text-right tabular-nums text-slate-600">
                      {entry.gross.display}
                    </td>
                    <td className="px-2 py-1.5 text-right font-medium tabular-nums">
                      {entry.recognized.minor > 0 ? entry.recognized.display : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {!run && items.length === 0 && !busy && (
        <p className="mt-4 text-xs text-slate-500">
          Collect evidence and reconcile cash first, then verify revenue.
        </p>
      )}
    </section>
  );
}
