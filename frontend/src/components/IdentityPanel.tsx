"use client";

/**
 * Feature 2 UI — resolved customers and the identity links behind them.
 *
 * The panel deliberately shows three things a reviewer needs and most tools hide:
 *
 * * **Aliases per customer** — which spellings were merged into one identity.
 * * **Prevented merges** — links refused because they would have combined entities
 *   with contradictory identifiers. A silent merge understates customer
 *   concentration, so refusing one is a finding worth showing.
 * * **Rejections, not just matches** — "why were these two NOT merged?" is answered
 *   with the same evidence trail used to accept a link.
 */

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { MatchProposal, ResolutionRun, ResolvedCustomer } from "@/lib/types";

const DECISION_STYLE: Record<string, string> = {
  ACCEPTED: "bg-emerald-100 text-emerald-800",
  REVIEW: "bg-amber-100 text-amber-800",
  REJECTED: "bg-slate-200 text-slate-700",
};

export function IdentityPanel({
  workspaceId,
  refreshKey,
  onChanged,
}: {
  workspaceId: string;
  /** Increments when a sibling panel changes server state. */
  refreshKey?: number;
  onChanged?: () => void;
}) {
  const [customers, setCustomers] = useState<ResolvedCustomer[]>([]);
  const [matches, setMatches] = useState<MatchProposal[]>([]);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [lastRun, setLastRun] = useState<ResolutionRun | null>(null);
  const [filter, setFilter] = useState<string>("REVIEW");
  const [useCritic, setUseCritic] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [customerData, matchData] = await Promise.all([
        api.listResolvedCustomers(workspaceId),
        api.listMatches(workspaceId, filter === "ALL" ? undefined : filter),
      ]);
      setCustomers(customerData.customers);
      setMatches(matchData.matches);
      setCounts(matchData.counts);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load identities");
    }
  }, [workspaceId, filter]);

  useEffect(() => {
    load();
    // refreshKey re-runs this when another panel has changed server state.
  }, [load, refreshKey]);

  async function resolve() {
    setBusy(true);
    setError(null);
    try {
      setLastRun(await api.resolveIdentities(workspaceId, useCritic));
      await load();
      onChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Resolution failed");
    } finally {
      setBusy(false);
    }
  }

  async function decide(match: MatchProposal, decision: "ACCEPTED" | "REJECTED") {
    const reason = window.prompt(
      `Reason for marking these ${decision === "ACCEPTED" ? "the same" : "different"} customers:`,
    );
    if (!reason) return;
    try {
      await api.decideMatch(workspaceId, match.id, decision, reason);
      await load();
      onChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not record the decision");
    }
  }

  return (
    <section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold">Customer identity</h2>
          <p className="mt-0.5 text-xs text-slate-600">
            Links records across contracts, accounting, CRM, payments and bank
            narrations into one customer — only when the evidence supports it.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <label className="flex items-center gap-1.5 text-xs text-slate-600">
            <input
              type="checkbox"
              checked={useCritic}
              onChange={(e) => setUseCritic(e.target.checked)}
              className="h-3.5 w-3.5"
            />
            independent critic
          </label>
          <button
            type="button"
            onClick={resolve}
            disabled={busy}
            className="rounded-md bg-slate-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-slate-800 disabled:opacity-50"
          >
            {busy ? "Resolving…" : "Resolve identities"}
          </button>
        </div>
      </div>

      {error && (
        <p role="alert" className="mt-3 rounded-md bg-rose-50 px-3 py-2 text-xs text-rose-700">
          {error}
        </p>
      )}

      {lastRun && (
        <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
          {[
            ["Records", lastRun.records_considered],
            ["Pairs scored", lastRun.pairs_generated],
            ["Customers", lastRun.clusters],
            ["Accepted", lastRun.accepted],
            ["For review", lastRun.review],
            ["Critic disputes", lastRun.critic_disputes],
          ].map(([label, value]) => (
            <div key={String(label)} className="rounded-md border border-slate-200 px-2 py-1.5">
              <p className="text-[11px] text-slate-500">{label}</p>
              <p className="text-base font-semibold tabular-nums">{value}</p>
            </div>
          ))}
        </div>
      )}

      {/* Prevented merges — the most important thing this feature does. */}
      {lastRun && lastRun.blocked_merges?.length > 0 && (
        <div className="mt-4 rounded-md border border-amber-200 bg-amber-50 p-3">
          <h3 className="text-xs font-semibold text-amber-900">
            {lastRun.blocked_merges.length} merges prevented
          </h3>
          <p className="mt-0.5 text-xs text-amber-800">
            These links were refused because they would have combined entities with
            contradictory identifiers. Merging them would understate customer
            concentration.
          </p>
          <ul className="mt-2 space-y-1">
            {lastRun.blocked_merges.slice(0, 5).map((blocked, index) => (
              <li key={index} className="text-xs text-amber-900">
                <span className="font-medium">
                  {blocked.would_have_merged?.join("  ↮  ")}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {lastRun && !lastRun.evaluation.auto_merge_permitted && (
        <p className="mt-3 rounded-md bg-slate-50 px-3 py-2 text-xs text-slate-600">
          {lastRun.evaluation.note ??
            "Automatic merging is disabled until measured precision clears the target."}
        </p>
      )}

      {/* Resolved customers with their aliases */}
      {customers.length > 0 && (
        <div className="mt-5">
          <h3 className="text-xs font-semibold text-slate-700">
            Resolved customers ({customers.length})
          </h3>
          <div className="mt-2 max-h-72 overflow-auto rounded border border-slate-200">
            <table className="w-full min-w-[600px] text-left text-xs">
              <thead className="sticky top-0 bg-slate-50 text-slate-500">
                <tr>
                  <th className="px-2 py-1 font-medium">Customer</th>
                  <th className="px-2 py-1 font-medium">Known as</th>
                  <th className="px-2 py-1 font-medium">Identifiers</th>
                </tr>
              </thead>
              <tbody>
                {customers.map((customer) => (
                  <tr key={customer.id} className="border-t border-slate-100 align-top">
                    <td className="px-2 py-1.5 font-medium">
                      {customer.canonical_name}
                      {customer.human_confirmed && (
                        <span className="ml-1 rounded bg-emerald-100 px-1 text-[10px] text-emerald-800">
                          confirmed
                        </span>
                      )}
                    </td>
                    <td className="px-2 py-1.5 text-slate-600">
                      {customer.known_aliases.length > 1
                        ? customer.known_aliases.join(" · ")
                        : "—"}
                    </td>
                    <td className="px-2 py-1.5 font-mono text-[11px] text-slate-500">
                      {[...customer.tax_identifiers, ...customer.domains].join(" ") || "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Match proposals, including rejections */}
      <div className="mt-5">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h3 className="text-xs font-semibold text-slate-700">Identity links</h3>
          <div className="flex gap-1">
            {["REVIEW", "ACCEPTED", "REJECTED", "ALL"].map((option) => (
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
                {option.toLowerCase()}
                {counts[option] !== undefined && ` (${counts[option]})`}
              </button>
            ))}
          </div>
        </div>

        {matches.length === 0 ? (
          <p className="mt-2 text-xs text-slate-500">
            No links in this category. Run resolution to populate them.
          </p>
        ) : (
          <ul className="mt-2 max-h-80 space-y-1 overflow-auto">
            {matches.slice(0, 40).map((match) => (
              <li key={match.id} className="rounded border border-slate-200 p-2">
                <button
                  type="button"
                  onClick={() => setExpanded(expanded === match.id ? null : match.id)}
                  className="flex w-full items-center justify-between gap-2 text-left"
                >
                  <span className="text-xs">
                    <span className="font-medium">{match.left.label}</span>
                    <span className="mx-1.5 text-slate-400">↔</span>
                    <span className="font-medium">{match.right.label}</span>
                  </span>
                  <span className="flex shrink-0 items-center gap-2">
                    <span className="text-[11px] tabular-nums text-slate-500">
                      {(match.score * 100).toFixed(0)}%
                    </span>
                    <span
                      className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${
                        DECISION_STYLE[match.decision]
                      }`}
                    >
                      {match.decision.toLowerCase()}
                    </span>
                  </span>
                </button>

                {expanded === match.id && (
                  <div className="mt-2 border-t border-slate-100 pt-2">
                    <table className="w-full text-[11px]">
                      <tbody>
                        {match.signals.map((signal, index) => (
                          <tr key={index}>
                            <td className="py-0.5 pr-2 text-slate-500">{signal.field}</td>
                            <td className="py-0.5 pr-2">{signal.outcome}</td>
                            <td
                              className={`py-0.5 pr-2 text-right tabular-nums ${
                                signal.weight < 0 ? "text-rose-600" : "text-emerald-700"
                              }`}
                            >
                              {signal.weight > 0 ? "+" : ""}
                              {signal.weight}
                            </td>
                            <td className="py-0.5 text-slate-600">{signal.detail}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                    {match.critic_note && (
                      <p className="mt-1.5 rounded bg-slate-50 px-2 py-1 text-[11px] text-slate-700">
                        <span className="font-medium">Critic:</span> {match.critic_note}
                      </p>
                    )}
                    {match.decision === "REVIEW" && (
                      <div className="mt-2 flex gap-2">
                        <button
                          type="button"
                          onClick={() => decide(match, "ACCEPTED")}
                          className="rounded bg-emerald-600 px-2 py-1 text-[11px] font-medium text-white hover:bg-emerald-700"
                        >
                          Same customer
                        </button>
                        <button
                          type="button"
                          onClick={() => decide(match, "REJECTED")}
                          className="rounded bg-slate-700 px-2 py-1 text-[11px] font-medium text-white hover:bg-slate-800"
                        >
                          Different customers
                        </button>
                      </div>
                    )}
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
