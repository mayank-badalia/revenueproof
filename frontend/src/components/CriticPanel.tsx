"use client";

/**
 * Feature 7 UI — the maker-checker, and the only thing that publishes a figure.
 *
 * Feature 5 proposes; this argues the other side before anyone is asked to look.
 * The screen has to make three things obvious, because they are what distinguish
 * this from a confidence score:
 *
 * * **Who settled it.** A dispute decided by arithmetic is a fact; one decided by
 *   the model is an argument. The badge says which, on every row, because a reader
 *   should weigh them differently.
 * * **Where it went.** A disputed item is routed back to the feature that owns the
 *   failure — identity, contracts, reconciliation — rather than into a general
 *   pile. Naming the destination is what makes the dispute actionable.
 * * **What is published.** Only an approved item carries the published mark. A
 *   disputed one keeps the classification Feature 5 gave it and simply stops being
 *   presented as a result; the critic never rewrites a financial figure.
 */

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { CriticDecisionRow, CriticRun } from "@/lib/types";

const VERDICT_STYLE: Record<string, string> = {
  APPROVED: "bg-emerald-100 text-emerald-900",
  DISPUTED: "bg-rose-100 text-rose-900",
  MORE_EVIDENCE_REQUIRED: "bg-amber-100 text-amber-900",
};

const VERDICT_LABEL: Record<string, string> = {
  APPROVED: "approved",
  DISPUTED: "disputed",
  MORE_EVIDENCE_REQUIRED: "needs evidence",
};

const FEATURE_NAME: Record<number, string> = {
  2: "identity resolution",
  3: "contract intelligence",
  4: "reconciliation",
  5: "revenue classification",
  6: "anomaly detection",
};

export function CriticPanel({
  workspaceId,
  refreshKey,
  onChanged,
}: {
  workspaceId: string;
  refreshKey?: number;
  onChanged?: () => void;
}) {
  const [run, setRun] = useState<CriticRun | null>(null);
  const [decisions, setDecisions] = useState<CriticDecisionRow[]>([]);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [filter, setFilter] = useState<string>("ALL");

  const load = useCallback(async () => {
    try {
      const body = await api.listCriticDecisions(workspaceId);
      setDecisions(body.decisions);
      setNote(body.note);
    } catch {
      // Stored verdicts are a convenience on mount; the run button reports its own
      // failures and is the primary action.
    }
  }, [workspaceId]);

  useEffect(() => {
    load();
  }, [load, refreshKey]);

  useEffect(() => {
    setRun(null);
  }, [workspaceId]);

  async function runCritic() {
    setBusy(true);
    setError(null);
    try {
      setRun(await api.runCritic(workspaceId));
      await load();
      onChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "The critic run failed");
    } finally {
      setBusy(false);
    }
  }

  const visible =
    filter === "ALL" ? decisions : decisions.filter((d) => d.verdict === filter);
  const counts = decisions.reduce<Record<string, number>>((acc, d) => {
    acc[d.verdict] = (acc[d.verdict] ?? 0) + 1;
    return acc;
  }, {});

  return (
    <section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold">Adversarial verification</h2>
          <p className="mt-0.5 max-w-2xl text-xs text-slate-600">
            An independent critic argues against every classification before it is
            published. Arithmetic runs first and cannot be overruled; a different
            model family reviews the material items that survive it. Nothing is
            published until it has been challenged.
          </p>
        </div>
        <button
          type="button"
          onClick={runCritic}
          disabled={busy}
          className="rounded-md bg-slate-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-slate-800 disabled:opacity-50"
        >
          {busy ? "Challenging…" : "Run critic"}
        </button>
      </div>

      {error && (
        <p role="alert" className="mt-3 rounded-md bg-rose-50 px-3 py-2 text-xs text-rose-700">
          {error}
        </p>
      )}

      {run && (
        <>
          <div className="mt-4 grid gap-3 sm:grid-cols-4">
            <div className="rounded-md border border-slate-300 bg-slate-50 p-3">
              <p className="text-[11px] uppercase tracking-wide text-slate-500">
                Published
              </p>
              <p className="mt-1 text-lg font-semibold tabular-nums text-emerald-700">
                {run.published}
              </p>
              <p className="mt-1 text-[11px] text-slate-600">
                of {run.items_reviewed} reviewed
              </p>
            </div>
            <div className="rounded-md border border-slate-300 bg-slate-50 p-3">
              <p className="text-[11px] uppercase tracking-wide text-slate-500">
                Disputed
              </p>
              <p className="mt-1 text-lg font-semibold tabular-nums text-rose-700">
                {run.disputed}
              </p>
              <p className="mt-1 text-[11px] text-slate-600">
                {run.more_evidence} need more evidence
              </p>
            </div>
            <div className="rounded-md border border-slate-300 bg-slate-50 p-3">
              <p className="text-[11px] uppercase tracking-wide text-slate-500">
                Settled by arithmetic
              </p>
              <p className="mt-1 text-lg font-semibold tabular-nums">
                {run.critic.settled_deterministically}
              </p>
              <p className="mt-1 text-[11px] text-slate-600">
                {run.critic.model_calls} model calls
              </p>
            </div>
            <div className="rounded-md border border-slate-300 bg-slate-50 p-3">
              <p className="text-[11px] uppercase tracking-wide text-slate-500">
                Sent back to
              </p>
              <p className="mt-1 text-[11px] text-slate-700">
                {Object.entries(run.critic.routed_to)
                  .map(
                    ([name, count]) =>
                      `${FEATURE_NAME[Number(name.split("_")[1])] ?? name} ${count}`,
                  )
                  .join(" · ") || "nothing disputed"}
              </p>
            </div>
          </div>

          {Object.keys(run.critic.by_issue).length > 0 && (
            <p className="mt-3 text-[11px] text-slate-600">
              <span className="font-medium">Issues raised: </span>
              {Object.entries(run.critic.by_issue)
                .map(([code, count]) => `${code.replace(/_/g, " ").toLowerCase()} ${count}`)
                .join(" · ")}
            </p>
          )}
        </>
      )}

      {note && (
        <p className="mt-3 rounded-md bg-slate-50 px-3 py-2 text-[11px] text-slate-600">
          {note}
        </p>
      )}

      {decisions.length > 0 && (
        <div className="mt-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h3 className="text-xs font-semibold text-slate-700">
              Verdicts ({decisions.length})
            </h3>
            <div className="flex flex-wrap gap-1">
              {["ALL", "DISPUTED", "MORE_EVIDENCE_REQUIRED", "APPROVED"]
                .filter((name) => name === "ALL" || counts[name])
                .map((name) => (
                  <button
                    key={name}
                    type="button"
                    onClick={() => setFilter(name)}
                    className={`rounded px-2 py-1 text-[11px] ${
                      filter === name
                        ? "bg-slate-900 text-white"
                        : "border border-slate-300 text-slate-600 hover:bg-slate-50"
                    }`}
                  >
                    {name === "ALL" ? "all" : VERDICT_LABEL[name]}
                    {name !== "ALL" && ` (${counts[name]})`}
                  </button>
                ))}
            </div>
          </div>

          <ul className="mt-2 max-h-[30rem] space-y-2 overflow-auto">
            {visible.map((decision) => (
              <li
                key={decision.id}
                className="rounded border border-slate-200 p-3 hover:bg-slate-50"
              >
                <button
                  type="button"
                  onClick={() =>
                    setExpanded(expanded === decision.id ? null : decision.id)
                  }
                  className="w-full text-left"
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-xs font-medium">{decision.description}</span>
                    <span
                      className={`rounded px-1.5 py-0.5 text-[10px] ${
                        VERDICT_STYLE[decision.verdict]
                      }`}
                    >
                      {VERDICT_LABEL[decision.verdict]}
                    </span>
                    {decision.is_published && (
                      <span className="rounded bg-emerald-50 px-1.5 py-0.5 text-[10px] text-emerald-800">
                        published
                      </span>
                    )}
                    {/* Which half decided this, because they carry different weight. */}
                    <span className="text-[10px] text-slate-500">
                      {decision.deterministic_findings.length
                        ? "settled by arithmetic"
                        : decision.critic_model
                          ? "argued by the critic model"
                          : "no issues found"}
                    </span>
                    {decision.routed_to_feature && (
                      <span className="text-[10px] text-sky-700">
                        → {FEATURE_NAME[decision.routed_to_feature]}
                      </span>
                    )}
                  </div>
                  {decision.issue_codes.length > 0 && (
                    <p className="mt-1 text-[11px] text-slate-700">
                      {decision.issue_codes
                        .map((code) => code.replace(/_/g, " ").toLowerCase())
                        .join(", ")}
                    </p>
                  )}
                </button>

                {expanded === decision.id && (
                  <div className="mt-2 space-y-1.5 border-t border-slate-100 pt-2 text-[11px]">
                    <p className="text-slate-700">{decision.reasoning}</p>
                    {decision.deterministic_findings.map((finding) => (
                      <p key={finding.code} className="text-slate-600">
                        <span className="font-mono text-[10px]">{finding.code}</span>{" "}
                        — {finding.detail}
                      </p>
                    ))}
                    {decision.requested_evidence.length > 0 && (
                      <p className="text-amber-800">
                        Evidence requested: {decision.requested_evidence.join("; ")}
                      </p>
                    )}
                    {decision.critic_model && (
                      <p className="font-mono text-[10px] text-slate-500">
                        {decision.critic_model}
                      </p>
                    )}
                  </div>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      {!run && decisions.length === 0 && !busy && (
        <p className="mt-4 text-xs text-slate-500">
          Verify revenue first, then run the critic. Nothing is published until it
          has survived a challenge.
        </p>
      )}
    </section>
  );
}
