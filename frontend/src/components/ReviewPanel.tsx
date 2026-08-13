"use client";

/**
 * Feature 7 UI — the queue where uncertainty gets settled.
 *
 * Every feature upstream routes what it could not decide to one place. Until this
 * screen existed those items accumulated with nowhere to go: the product's stated
 * safe answer, `HUMAN_REVIEW`, had no human attached to it.
 *
 * Two things the design insists on:
 *
 * * **A decision cannot be recorded without a reason.** The button is disabled
 *   until one is typed. An override with no reason is how a figure becomes
 *   unauditable — a later reader sees the number moved and cannot ask why.
 * * **The item says who is asking.** "Feature 2 — identity resolution" beside the
 *   title, because "Prevented false merge: Blue Harbor ↔ Blue Harbour" means
 *   something quite different depending on which engine raised it.
 *
 * Reviewers who cannot resolve still see everything. Reading is not the privilege
 * being protected; moving a material figure is.
 */

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { AnomalySeverityName, ReviewQueue, ReviewItemRow } from "@/lib/types";

const SEVERITY_STYLE: Record<AnomalySeverityName, string> = {
  high: "bg-rose-100 text-rose-900",
  medium: "bg-amber-100 text-amber-900",
  low: "bg-slate-200 text-slate-700",
  info: "bg-sky-100 text-sky-900",
};

const DECISION_LABEL: Record<string, string> = {
  approved: "Approve",
  rejected: "Reject",
  corrected: "Correct",
};

const DECISION_STYLE: Record<string, string> = {
  approved: "border-emerald-300 text-emerald-800 hover:bg-emerald-50",
  rejected: "border-rose-300 text-rose-800 hover:bg-rose-50",
  corrected: "border-sky-300 text-sky-800 hover:bg-sky-50",
};

export function ReviewPanel({
  workspaceId,
  refreshKey,
  onChanged,
}: {
  workspaceId: string;
  refreshKey?: number;
  onChanged?: () => void;
}) {
  const [queue, setQueue] = useState<ReviewQueue | null>(null);
  const [statusFilter, setStatusFilter] = useState("open");
  const [expanded, setExpanded] = useState<string | null>(null);
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setQueue(await api.listReview(workspaceId, statusFilter));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load the review queue");
    }
  }, [workspaceId, statusFilter]);

  useEffect(() => {
    load();
  }, [load, refreshKey]);

  async function decide(item: ReviewItemRow, decision: string) {
    const reason = (reasons[item.id] ?? "").trim();
    if (!reason) return;
    setBusy(item.id);
    setError(null);
    try {
      await api.resolveReview(workspaceId, item.id, decision, reason);
      setReasons((current) => ({ ...current, [item.id]: "" }));
      setExpanded(null);
      await load();
      onChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not record the decision");
    } finally {
      setBusy(null);
    }
  }

  const summary = queue?.summary;

  return (
    <section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold">Review queue</h2>
          <p className="mt-0.5 max-w-2xl text-xs text-slate-600">
            Everything the pipeline refused to decide on its own. Equivalent
            questions are collapsed into one decision — answering it resolves every
            record it covers, each with its own audit entry. A decision must state a
            reason.
          </p>
        </div>
        <div className="flex flex-wrap gap-1">
          {["open", "resolved", "dismissed", "all"].map((name) => (
            <button
              key={name}
              type="button"
              onClick={() => setStatusFilter(name)}
              className={`rounded px-2 py-1 text-[11px] ${
                statusFilter === name
                  ? "bg-slate-900 text-white"
                  : "border border-slate-300 text-slate-600 hover:bg-slate-50"
              }`}
            >
              {name}
            </button>
          ))}
        </div>
      </div>

      {error && (
        <p role="alert" className="mt-3 rounded-md bg-rose-50 px-3 py-2 text-xs text-rose-700">
          {error}
        </p>
      )}

      {summary && (
        <div className="mt-4 grid gap-3 sm:grid-cols-4">
          <div className="rounded-md border border-slate-300 bg-slate-50 p-3">
            <p className="text-[11px] uppercase tracking-wide text-slate-500">
              Decisions to make
            </p>
            <p className="mt-1 text-lg font-semibold tabular-nums">
              {summary.open_decisions}
            </p>
            <p className="mt-1 text-[11px] text-slate-600">
              covering {summary.open} records
              {summary.oldest_open_days !== null &&
                ` · oldest ${summary.oldest_open_days}d`}
            </p>
          </div>
          <div className="rounded-md border border-slate-300 bg-slate-50 p-3">
            <p className="text-[11px] uppercase tracking-wide text-slate-500">Resolved</p>
            <p className="mt-1 text-lg font-semibold tabular-nums text-emerald-700">
              {summary.resolved}
            </p>
          </div>
          <div className="rounded-md border border-slate-300 bg-slate-50 p-3">
            <p className="text-[11px] uppercase tracking-wide text-slate-500">Dismissed</p>
            <p className="mt-1 text-lg font-semibold tabular-nums">{summary.dismissed}</p>
          </div>
          <div className="rounded-md border border-slate-300 bg-slate-50 p-3">
            <p className="text-[11px] uppercase tracking-wide text-slate-500">Raised by</p>
            <p className="mt-1 text-[11px] text-slate-700">
              {Object.entries(summary.by_category)
                .map(([name, count]) => `${name.replace(/_/g, " ")} ${count}`)
                .join(" · ") || "—"}
            </p>
          </div>
        </div>
      )}

      {queue && !queue.can_resolve && queue.items.length > 0 && (
        <p className="mt-3 rounded-md bg-sky-50 px-3 py-2 text-[11px] text-sky-900">
          Your role can read and comment but not resolve. Material decisions are
          restricted to the workspace owner and analysts.
        </p>
      )}

      {queue && queue.items.length === 0 && (
        <p className="mt-4 text-xs text-slate-500">
          Nothing in this queue. Run the pipeline — anything it cannot decide with
          confidence arrives here rather than being guessed.
        </p>
      )}

      <ul className="mt-4 space-y-2">
        {queue?.items.map((item) => (
          <li key={item.id} className="rounded border border-slate-200 p-3">
            <button
              type="button"
              onClick={() => setExpanded(expanded === item.id ? null : item.id)}
              className="w-full text-left"
            >
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-xs font-medium">{item.title}</span>
                <span
                  className={`rounded px-1.5 py-0.5 text-[10px] ${
                    SEVERITY_STYLE[item.severity] ?? SEVERITY_STYLE.low
                  }`}
                >
                  {item.severity}
                </span>
                <span className="text-[10px] text-slate-500">{item.raised_by}</span>
                {item.member_count > 1 && (
                  <span className="rounded bg-sky-100 px-1.5 py-0.5 text-[10px] text-sky-900">
                    {item.member_count} records, one decision
                  </span>
                )}
                {item.resolution && (
                  <span className="rounded bg-slate-200 px-1.5 py-0.5 text-[10px] text-slate-700">
                    {item.resolution}
                  </span>
                )}
              </div>
              <p className="mt-1 text-[11px] text-slate-700">{item.detail}</p>
            </button>

            {expanded === item.id && (
              <div className="mt-2 space-y-2 border-t border-slate-100 pt-2">
                {item.also_affects.length > 0 && (
                  <div className="text-[11px] text-slate-600">
                    <span className="font-medium">
                      Answering this also resolves:
                    </span>
                    <ul className="mt-0.5 list-disc pl-4">
                      {item.also_affects.map((title) => (
                        <li key={title}>{title}</li>
                      ))}
                      {item.member_count - 1 > item.also_affects.length && (
                        <li>
                          and {item.member_count - 1 - item.also_affects.length} more
                        </li>
                      )}
                    </ul>
                  </div>
                )}

                {item.resolution_reason && (
                  <p className="text-[11px] text-slate-600">
                    <span className="font-medium">Reason given: </span>
                    {item.resolution_reason}
                  </p>
                )}

                {Object.keys(item.evidence_packet ?? {}).length > 0 && (
                  <details className="text-[11px] text-slate-600">
                    <summary className="cursor-pointer">Evidence packet</summary>
                    <pre className="mt-1 max-h-56 overflow-auto rounded bg-slate-50 p-2 text-[10px] leading-relaxed">
                      {JSON.stringify(item.evidence_packet, null, 2)}
                    </pre>
                  </details>
                )}

                {queue?.can_resolve && item.status === "open" && (
                  <div className="space-y-2">
                    <textarea
                      value={reasons[item.id] ?? ""}
                      onChange={(event) =>
                        setReasons((current) => ({
                          ...current,
                          [item.id]: event.target.value,
                        }))
                      }
                      placeholder="Why? This is stored in the audit chain and cannot be left blank."
                      rows={2}
                      className="w-full rounded border border-slate-300 px-2 py-1.5 text-[11px]"
                    />
                    <div className="flex flex-wrap gap-2">
                      {(queue?.decisions ?? []).map((decision) => (
                        <button
                          key={decision}
                          type="button"
                          disabled={
                            busy === item.id || !(reasons[item.id] ?? "").trim()
                          }
                          onClick={() => decide(item, decision)}
                          className={`rounded border px-2.5 py-1 text-[11px] disabled:cursor-not-allowed disabled:opacity-40 ${
                            DECISION_STYLE[decision] ?? "border-slate-300"
                          }`}
                        >
                          {DECISION_LABEL[decision] ?? decision}
                        </button>
                      ))}
                      {!(reasons[item.id] ?? "").trim() && (
                        <span className="self-center text-[10px] text-slate-500">
                          a reason is required
                        </span>
                      )}
                    </div>
                  </div>
                )}
              </div>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
