"use client";

/**
 * Run the agents — one stage, or the whole chain.
 *
 * The individual panels each own a button, which is the right control when you are
 * inspecting one stage and the wrong one when you just want the answer. This is the
 * other door.
 *
 * The interesting behaviour is what it refuses. With no evidence loaded, "Run
 * everything" does not run: every stage would succeed over an empty workspace and
 * the room would report a claim proven at 0%, which reads exactly like a claim that
 * was checked and failed. Instead the panel says so and offers the choice of
 * datasets, so the answer to "there is no data" is a button rather than an error.
 */

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { PipelineRun, PipelineState } from "@/lib/types";

export function RunPanel({
  workspaceId,
  refreshKey,
  onChanged,
  onNeedsData,
}: {
  workspaceId: string;
  refreshKey?: number;
  onChanged?: () => void;
  /** Send the reader to the evidence-source chooser rather than describing it. */
  onNeedsData?: () => void;
}) {
  const [state, setState] = useState<PipelineState | null>(null);
  const [run, setRun] = useState<PipelineRun | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setState(await api.pipelineState(workspaceId));
    } catch {
      // The panel is still usable without the catalogue; the run itself reports
      // anything that is actually wrong.
    }
  }, [workspaceId]);

  useEffect(() => {
    load();
  }, [load, refreshKey]);

  async function start(stages?: string[]) {
    setBusy(stages ? stages[0] : "all");
    setError(null);
    setRun(null);
    try {
      const result = await api.runPipeline(workspaceId, stages);
      setRun(result);
      await load();
      onChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "The run failed");
    } finally {
      setBusy(null);
    }
  }

  const hasEvidence = state?.has_evidence ?? false;
  const completed = state?.completed ?? {};
  const stages = state?.stages ?? [];
  const everythingRan = stages.length > 0 && stages.every((s) => s.has_run);

  return (
    <section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold">Run the analysis</h2>
          <p className="mt-0.5 max-w-2xl text-xs text-slate-600">
            Run one stage on its own, or the whole chain in order. Stages run in
            dependency order rather than together — identity decides who a customer
            is, and every figure above it is counted per customer.
          </p>
        </div>
        <button
          type="button"
          onClick={() => (hasEvidence ? start() : onNeedsData?.())}
          disabled={busy !== null}
          className="rounded-md bg-slate-900 px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50"
        >
          {busy === "all" ? "Running…" : "Run everything"}
        </button>
      </div>

      {/* No evidence is a question with an answer, not an error to report after
          the click. */}
      {state && !hasEvidence && (
        <div className="mt-4 rounded-md border border-amber-300 bg-amber-50 p-3">
          <p className="text-xs font-medium text-amber-900">
            Nothing to run on yet — this workspace has no evidence.
          </p>
          <p className="mt-1 text-[11px] text-amber-900">
            Every stage would succeed over an empty workspace and the room would
            report the claim proven at 0%, which reads exactly like a claim that was
            checked and failed. Load records first:
          </p>
          <button
            type="button"
            onClick={onNeedsData}
            className="mt-2 rounded-md bg-amber-900 px-3 py-1.5 text-[11px] font-medium text-white"
          >
            Choose an evidence source
          </button>
        </div>
      )}

      {error && (
        <p role="alert" className="mt-3 rounded-md bg-rose-50 px-3 py-2 text-xs text-rose-700">
          {error}
        </p>
      )}

      {run?.blocked && (
        <div className="mt-3 rounded-md border border-amber-300 bg-amber-50 px-3 py-2">
          <p className="text-xs font-medium text-amber-900">{run.blocked}</p>
          {run.remedy && (
            <p className="mt-0.5 text-[11px] text-amber-900">{run.remedy}</p>
          )}
        </div>
      )}

      {run && !run.blocked && (
        <div
          className={`mt-3 rounded-md px-3 py-2 text-xs ${
            run.ok ? "bg-emerald-50 text-emerald-800" : "bg-rose-50 text-rose-700"
          }`}
        >
          {run.stages_run} stage{run.stages_run === 1 ? "" : "s"} completed in{" "}
          {run.seconds.toFixed(1)}s
          {run.stages_failed > 0 && `, ${run.stages_failed} failed`}.
          {run.stages_failed > 0 &&
            " The run stopped there rather than computing later figures from work that had just failed."}
        </div>
      )}

      <div className="mt-4 space-y-2">
        {stages.map((stage) => {
          const outcome = run?.stages.find((s) => s.key === stage.key);
          const blockedBy = stage.needs.filter((n) => !completed[n]);
          return (
            <div
              key={stage.key}
              className="flex flex-wrap items-start justify-between gap-3 rounded-md border border-slate-200 p-3"
            >
              <div className="min-w-0 flex-1">
                <p className="text-xs font-medium">
                  <span className="mr-2 rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] text-slate-600">
                    F{stage.feature}
                  </span>
                  {stage.label}
                  {stage.has_run && (
                    <span className="ml-2 rounded bg-emerald-100 px-1.5 py-0.5 text-[10px] font-normal text-emerald-800">
                      has run
                    </span>
                  )}
                </p>
                <p className="mt-0.5 text-[11px] text-slate-600">{stage.purpose}</p>
                {outcome && (
                  <p
                    className={`mt-1 text-[11px] ${
                      outcome.status === "failed"
                        ? "text-rose-700"
                        : "text-slate-700"
                    }`}
                  >
                    {outcome.detail} · {outcome.seconds.toFixed(1)}s
                  </p>
                )}
                {!stage.has_run && blockedBy.length > 0 && (
                  <p className="mt-1 text-[11px] text-amber-800">
                    Needs {blockedBy.join(", ")} first.
                  </p>
                )}
              </div>
              <button
                type="button"
                onClick={() => start([stage.key])}
                disabled={busy !== null || !hasEvidence || blockedBy.length > 0}
                title={
                  blockedBy.length > 0
                    ? `Run ${blockedBy.join(", ")} first, or run everything.`
                    : undefined
                }
                className="shrink-0 rounded-md border border-slate-300 px-2.5 py-1 text-[11px] font-medium disabled:opacity-40"
              >
                {busy === stage.key ? "Running…" : "Run this"}
              </button>
            </div>
          );
        })}
      </div>

      {state && hasEvidence && !everythingRan && (
        <p className="mt-3 text-[11px] text-slate-500">
          Stages without a “has run” mark have never produced anything on this
          workspace. “Run everything” covers all of them in order.
        </p>
      )}
    </section>
  );
}
