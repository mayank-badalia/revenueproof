"use client";

/**
 * Feature 4 UI — the cash chain, invoice by invoice.
 *
 * Three things are shown separately that most tools merge into one "paid" flag,
 * because merging them is what lets unsupported revenue hide:
 *
 * * **Allocated** — cash was applied to this invoice.
 * * **Bank confirmed** — the money actually reached the bank. "Captured" is not
 *   "settled"; a processor payment can be captured and never arrive.
 * * **Retained** — what survived refunds and chargebacks.
 *
 * The conservation check is surfaced at the top. If allocated + outstanding does not
 * equal invoiced to the exact paisa, every figure below it is untrustworthy and the
 * panel says so rather than rendering a confident total.
 */

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { InvoiceOutcome, ReconciliationRun } from "@/lib/types";

function formatMinor(minor: number, currency = "INR"): string {
  // Display only — the backend owns every figure. Dividing here is presentation,
  // never arithmetic that feeds a decision.
  return `${currency} ${(minor / 100).toLocaleString("en-IN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

export function ReconciliationPanel({
  workspaceId,
  refreshKey,
  onChanged,
}: {
  workspaceId: string;
  refreshKey?: number;
  onChanged?: () => void;
}) {
  const [run, setRun] = useState<ReconciliationRun | null>(null);
  const [busy, setBusy] = useState(false);
  const [restoring, setRestoring] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<"all" | "issues">("issues");

  // Reconciliation is derived state: the allocations persist, the per-invoice view
  // does not. Saying "collect evidence first" over a workspace that has already been
  // reconciled reads as the feature never having run, so the stored position is
  // fetched back on mount and recomputed server-side through the same function the
  // button calls.
  //
  // It *does* re-run on `refreshKey`, because "Run everything" reconciles without
  // this panel knowing, and a panel that only refreshes on mount then reads
  // "collect evidence first" over a reconciliation that just happened.
  //
  // The result is never cleared before its replacement arrives. That is what made
  // the original version wipe itself: reconcile() calls onChanged(), the page bumps
  // dataVersion, the effect fired and set run back to null, and the figure vanished
  // the instant it appeared. Showing the previous result for the second it takes to
  // fetch the next one is strictly better than showing nothing.
  useEffect(() => {
    let cancelled = false;
    setRestoring(true);
    api
      .reconciliation(workspaceId)
      .then((stored) => {
        if (cancelled) return;
        if (stored.reconciled) setRun(stored);
        else setRun(null);
      })
      // A failed restore is not a failed reconciliation — leave the panel as it is
      // rather than showing an error for a background fetch.
      .catch(() => undefined)
      .finally(() => {
        if (!cancelled) setRestoring(false);
      });
    return () => {
      cancelled = true;
    };
  }, [workspaceId, refreshKey]);

  // A different workspace is a different subject: drop the old figure immediately
  // rather than showing one company's cash under another's name.
  useEffect(() => {
    setRun(null);
  }, [workspaceId]);

  async function reconcile() {
    setBusy(true);
    setError(null);
    try {
      setRun(await api.reconcile(workspaceId));
      onChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Reconciliation failed");
    } finally {
      setBusy(false);
    }
  }

  const outcomes: InvoiceOutcome[] = run
    ? filter === "all"
      ? run.outcomes
      : run.outcomes.filter(
          (o) =>
            o.outstanding_minor > 0 ||
            o.refunded_minor > 0 ||
            !o.bank_confirmed,
        )
    : [];

  return (
    <section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold">Cash reconciliation</h2>
          <p className="mt-0.5 text-xs text-slate-600">
            Matches invoices to payments to bank receipts, subtracts refunds and
            chargebacks, and reports what was actually received and kept. Runs
            entirely on deterministic rules — no model calls.
          </p>
        </div>
        <button
          type="button"
          onClick={reconcile}
          disabled={busy}
          className="rounded-md bg-slate-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-slate-800 disabled:opacity-50"
        >
          {busy ? "Reconciling…" : "Reconcile cash"}
        </button>
      </div>

      {error && (
        <p role="alert" className="mt-3 rounded-md bg-rose-50 px-3 py-2 text-xs text-rose-700">
          {error}
        </p>
      )}

      {run && (
        <>
          {/* Conservation first. If value was created or destroyed, nothing below
              this line can be relied on, so it is stated before any total. */}
          <div
            className={`mt-4 rounded-md px-3 py-2 text-xs ${
              run.conservation_ok
                ? "bg-emerald-50 text-emerald-900"
                : "bg-rose-50 text-rose-900"
            }`}
          >
            {run.conservation_ok ? (
              <>
                <span className="font-semibold">Conservation verified</span> — allocated
                plus outstanding equals invoiced, to the exact paisa. Solver status:{" "}
                {run.solver_status}.
              </>
            ) : (
              <>
                <span className="font-semibold">Conservation FAILED</span> —{" "}
                {run.conservation_error}. No figure from this run should be relied on.
              </>
            )}
          </div>

          <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
            {[
              ["Invoiced", run.totals.invoiced],
              ["Allocated", run.totals.allocated],
              ["Outstanding", run.totals.outstanding],
              ["Refunded", run.totals.refunded],
              ["Retained", run.totals.retained],
              ["Bank confirmed", run.totals.bank_confirmed],
            ].map(([label, money]) => (
              <div
                key={String(label)}
                className="rounded-md border border-slate-200 px-2 py-1.5"
              >
                <p className="text-[11px] text-slate-500">{String(label)}</p>
                <p className="text-sm font-semibold tabular-nums">
                  {money ? (money as { display: string }).display : "—"}
                </p>
              </div>
            ))}
          </div>

          <div className="mt-3 flex flex-wrap gap-3 text-xs text-slate-600">
            <span>{run.allocations_written} allocations</span>
            <span>·</span>
            <span>{run.candidate_links} candidates considered</span>
            <span>·</span>
            <span
              className={run.failed_payments > 0 ? "text-amber-700" : ""}
            >
              {run.failed_payments} failed payments (contribute zero)
            </span>
            <span>·</span>
            <span className={run.unsupported_receipts > 0 ? "text-rose-700" : ""}>
              {run.unsupported_receipts} receipts with no payment behind them
            </span>
            <span>·</span>
            <span>{run.invoices_unpaid} invoices with no payment</span>
          </div>

          {run.unapplied_cash.minor > 0 && (
            <p className="mt-3 rounded-md bg-amber-50 px-3 py-2 text-xs text-amber-900">
              <span className="font-semibold">
                {run.unapplied_cash.display} of received cash is unapplied.
              </span>{" "}
              It could not be matched to any invoice. Unapplied money is an open
              question, not revenue.
            </p>
          )}

          <div className="mt-5">
            <div className="flex items-center justify-between">
              <h3 className="text-xs font-semibold text-slate-700">
                Per-invoice outcome
              </h3>
              <div className="flex gap-1">
                {(["issues", "all"] as const).map((option) => (
                  <button
                    key={option}
                    type="button"
                    onClick={() => setFilter(option)}
                    className={`rounded px-2 py-1 text-[11px] ${
                      filter === option
                        ? "bg-slate-900 text-white"
                        : "border border-slate-300 text-slate-600 hover:bg-slate-50"
                    }`}
                  >
                    {option === "issues" ? "needs attention" : "all"}
                  </button>
                ))}
              </div>
            </div>

            <div className="mt-2 max-h-96 overflow-auto rounded border border-slate-200">
              <table className="w-full min-w-[820px] text-left text-xs">
                <thead className="sticky top-0 bg-slate-50 text-slate-500">
                  <tr>
                    <th className="px-2 py-1.5 font-medium">Invoice</th>
                    <th className="px-2 py-1.5 font-medium">Customer</th>
                    <th className="px-2 py-1.5 text-right font-medium">Total</th>
                    <th className="px-2 py-1.5 text-right font-medium">Allocated</th>
                    <th className="px-2 py-1.5 text-right font-medium">Outstanding</th>
                    <th className="px-2 py-1.5 text-right font-medium">Refunded</th>
                    <th className="px-2 py-1.5 text-right font-medium">Retained</th>
                    <th className="px-2 py-1.5 font-medium">Bank</th>
                  </tr>
                </thead>
                <tbody>
                  {outcomes.map((outcome) => (
                    <tr
                      key={outcome.invoice_id}
                      className="border-t border-slate-100 align-top"
                    >
                      <td className="px-2 py-1.5 font-medium">
                        {outcome.invoice_number ?? "—"}
                        {outcome.notes.length > 0 && (
                          <p className="mt-0.5 font-normal text-[11px] text-slate-500">
                            {outcome.notes.join(" ")}
                          </p>
                        )}
                      </td>
                      <td className="px-2 py-1.5 text-slate-600">
                        {outcome.customer ?? "—"}
                      </td>
                      <td className="px-2 py-1.5 text-right tabular-nums">
                        {formatMinor(outcome.total_minor, outcome.currency)}
                      </td>
                      <td className="px-2 py-1.5 text-right tabular-nums">
                        {formatMinor(outcome.allocated_minor, outcome.currency)}
                      </td>
                      <td
                        className={`px-2 py-1.5 text-right tabular-nums ${
                          outcome.outstanding_minor > 0 ? "text-amber-700" : "text-slate-400"
                        }`}
                      >
                        {outcome.outstanding_minor > 0
                          ? formatMinor(outcome.outstanding_minor, outcome.currency)
                          : "—"}
                      </td>
                      <td
                        className={`px-2 py-1.5 text-right tabular-nums ${
                          outcome.refunded_minor > 0 ? "text-rose-700" : "text-slate-400"
                        }`}
                      >
                        {outcome.refunded_minor > 0
                          ? formatMinor(outcome.refunded_minor, outcome.currency)
                          : "—"}
                      </td>
                      <td className="px-2 py-1.5 text-right font-medium tabular-nums">
                        {formatMinor(outcome.retained_minor, outcome.currency)}
                      </td>
                      <td className="px-2 py-1.5">
                        {outcome.bank_confirmed ? (
                          <span className="rounded bg-emerald-100 px-1.5 py-0.5 text-[10px] text-emerald-800">
                            confirmed
                          </span>
                        ) : outcome.allocated_minor > 0 ? (
                          <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] text-amber-800">
                            not settled
                          </span>
                        ) : (
                          <span className="text-slate-400">—</span>
                        )}
                      </td>
                    </tr>
                  ))}
                  {outcomes.length === 0 && (
                    <tr>
                      <td colSpan={8} className="px-2 py-3 text-slate-500">
                        {filter === "issues"
                          ? "Every invoice is fully settled and bank-confirmed."
                          : "No invoices to reconcile."}
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}

      {!run && !busy && (
        <p className="mt-4 text-xs text-slate-500">
          {restoring
            ? "Restoring the reconciled position…"
            : "Collect evidence first, then reconcile to see what cash actually arrived."}
        </p>
      )}
    </section>
  );
}
