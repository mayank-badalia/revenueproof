"use client";

/**
 * Feature 6 UI — where a reviewer decides what to look at first.
 *
 * The screen's job is to spend a person's attention well, so it is built around
 * three constraints:
 *
 * * **Nothing here accuses anyone.** Every finding is an indicator requiring
 *   review, the disclaimer is always on screen rather than tucked into a tooltip,
 *   and the word "fraud" is not in this file. A flag says what was observed, what
 *   normal looks like, and what a human should go and check.
 * * **Observed sits beside baseline, always.** "₹59,000 captured twice" means
 *   nothing without "one payment per charge within a day" next to it. A number with
 *   no comparison is not an argument, and a reviewer cannot disagree with it.
 * * **Every finding can be pushed back on.** The false-positive control is not a
 *   nicety: it is the only source of the precision measurement, and the model is
 *   switched off by its own record rather than by opinion. So the panel shows the
 *   measured precision and the gate's current decision in plain words.
 *
 * Severity drives order, not recency. A queue sorted by when the scan happened to
 * write a row buries the serious flag under the routine ones.
 */

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { AnomalyFinding, AnomalyPrecision, AnomalyRun, AnomalySeverityName } from "@/lib/types";

const SEVERITY_STYLE: Record<AnomalySeverityName, string> = {
  high: "bg-rose-100 text-rose-900",
  medium: "bg-amber-100 text-amber-900",
  low: "bg-slate-200 text-slate-700",
  info: "bg-sky-100 text-sky-900",
};

const SEVERITY_LABEL: Record<AnomalySeverityName, string> = {
  high: "high",
  medium: "medium",
  low: "low",
  info: "info",
};

export function AnomalyPanel({
  workspaceId,
  refreshKey,
  onChanged,
}: {
  workspaceId: string;
  refreshKey?: number;
  onChanged?: () => void;
}) {
  const [run, setRun] = useState<AnomalyRun | null>(null);
  const [findings, setFindings] = useState<AnomalyFinding[]>([]);
  const [precision, setPrecision] = useState<AnomalyPrecision | null>(null);
  const [disclaimer, setDisclaimer] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [severityFilter, setSeverityFilter] = useState<AnomalySeverityName | "ALL">("ALL");

  const loadFindings = useCallback(async () => {
    try {
      const body = await api.listAnomalies(workspaceId);
      setFindings(body.anomalies);
      setDisclaimer(body.disclaimer);
      setPrecision(await api.anomalyPrecision(workspaceId));
    } catch {
      // Stored findings are a convenience on mount; the scan button is the
      // primary action and reports its own failures.
    }
  }, [workspaceId]);

  useEffect(() => {
    loadFindings();
  }, [loadFindings, refreshKey]);

  useEffect(() => {
    setRun(null);
  }, [workspaceId]);

  async function scan() {
    setBusy(true);
    setError(null);
    try {
      setRun(await api.scanAnomalies(workspaceId));
      await loadFindings();
      onChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Anomaly scan failed");
    } finally {
      setBusy(false);
    }
  }

  async function judge(anomalyId: string, isFalsePositive: boolean) {
    try {
      const body = await api.anomalyFeedback(workspaceId, anomalyId, isFalsePositive);
      setPrecision(body.precision);
      setFindings((current) =>
        current.map((row) =>
          row.id === anomalyId
            ? { ...row, is_false_positive: body.is_false_positive, status: body.status }
            : row,
        ),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not record feedback");
    }
  }

  const visible =
    severityFilter === "ALL"
      ? findings
      : findings.filter((row) => row.severity === severityFilter);

  const counts = findings.reduce<Record<string, number>>((acc, row) => {
    acc[row.severity] = (acc[row.severity] ?? 0) + 1;
    return acc;
  }, {});

  return (
    <section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold">Anomaly indicators</h2>
          <p className="mt-0.5 max-w-2xl text-xs text-slate-600">
            Deterministic rules, an explainable model and a graph search over related
            parties and circular flows, run independently and joined. Each item says
            what was observed, what this company&apos;s own baseline is, and what a
            reviewer should check.
          </p>
        </div>
        <button
          type="button"
          onClick={scan}
          disabled={busy}
          className="rounded-md bg-slate-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-slate-800 disabled:opacity-50"
        >
          {busy ? "Scanning…" : "Scan for anomalies"}
        </button>
      </div>

      {/* Always visible, never behind an interaction. */}
      <p className="mt-3 rounded-md bg-slate-50 px-3 py-2 text-[11px] text-slate-600">
        {disclaimer ||
          "Every item is an anomaly indicator requiring review. None of them is a finding of wrongdoing."}
      </p>

      {error && (
        <p role="alert" className="mt-3 rounded-md bg-rose-50 px-3 py-2 text-xs text-rose-700">
          {error}
        </p>
      )}

      {run && (
        <>
          <div className="mt-4 grid gap-3 sm:grid-cols-3">
            <div className="rounded-md border border-slate-300 bg-slate-50 p-3">
              <p className="text-[11px] uppercase tracking-wide text-slate-500">
                Indicators raised
              </p>
              <p className="mt-1 text-lg font-semibold tabular-nums">
                {run.findings_total}
              </p>
              <p className="mt-1 text-[11px] text-slate-600">
                {run.by_severity.high ?? 0} high · {run.review_items_created} sent to
                review
              </p>
            </div>

            <div className="rounded-md border border-slate-300 bg-slate-50 p-3">
              <p className="text-[11px] uppercase tracking-wide text-slate-500">
                Customer concentration
              </p>
              <p className="mt-1 text-lg font-semibold tabular-nums">
                {run.concentration.top_share_pct.toFixed(1)}%
              </p>
              <p className="mt-1 text-[11px] text-slate-600">
                {run.concentration.top_customer ?? "—"} · top {run.concentration.top_n}{" "}
                hold {run.concentration.top_n_share_pct.toFixed(1)}% · HHI{" "}
                {run.concentration.hhi.toFixed(0)}
              </p>
            </div>

            <div className="rounded-md border border-slate-300 bg-slate-50 p-3">
              <p className="text-[11px] uppercase tracking-wide text-slate-500">
                Graph investigation
              </p>
              <p className="mt-1 text-lg font-semibold tabular-nums">
                {run.graph.cycles.length}
              </p>
              <p className="mt-1 text-[11px] text-slate-600">
                circular flows · {run.graph.clusters.length} related-party clusters ·{" "}
                {run.graph.method}
              </p>
            </div>
          </div>

          {/* The HHI caveat travels with the number, because the antitrust bands
              it borrows from do not describe a startup. */}
          <p className="mt-2 text-[11px] text-slate-500">
            {run.concentration.hhi_caveat}
          </p>

          {/* Whether the model ran, and why — never left to be inferred. */}
          <div
            className={`mt-3 rounded-md px-3 py-2 text-[11px] ${
              run.ml.enabled ? "bg-sky-50 text-sky-900" : "bg-amber-50 text-amber-900"
            }`}
          >
            <span className="font-medium">
              Statistical model {run.ml.enabled ? "ran" : "did not run"}.
            </span>{" "}
            {run.ml.reason}.
            {run.ml.model_version && (
              <span className="ml-1 font-mono text-[10px] text-slate-500">
                {run.ml.model_version}
              </span>
            )}
            {run.ml.validation?.drift_suspected && (
              <span className="ml-1 font-medium">
                {" "}
                Flag rate moved sharply between time-ordered folds, which points at a
                change in the workspace rather than at more findings.
              </span>
            )}
          </div>

          {run.narrative.attempted > 0 && (
            <p className="mt-2 text-[11px] text-slate-500">
              Narratives: {run.narrative.written} written, {run.narrative.rejected}{" "}
              rejected for wording, {run.narrative.skipped} skipped
              {run.narrative.failed ? `, ${run.narrative.failed} failed` : ""} of{" "}
              {run.narrative.eligible} material findings. The packet below is the
              finding; the prose is only a restatement of it.
            </p>
          )}

          {run.graph.cycles.length > 0 && (
            <div className="mt-3 rounded-md border border-rose-200 bg-rose-50 p-3">
              <p className="text-xs font-semibold text-rose-900">
                Funds returning to their origin
              </p>
              {run.graph.cycles.map((cycle) => (
                <p key={cycle.path.join("→")} className="mt-1 text-[11px] text-rose-800">
                  {cycle.path.join(" → ")} → {cycle.path[0]}
                </p>
              ))}
              <p className="mt-1 text-[10px] text-rose-700">
                A rebate or an intercompany settlement produces a legitimate loop. The
                dates and direction of each leg decide it.
              </p>
            </div>
          )}
        </>
      )}

      {/* Measured precision, and the gate decision that follows from it. */}
      {precision && precision.total_findings > 0 && (
        <div className="mt-4 rounded-md border border-slate-200 p-3">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <h3 className="text-xs font-semibold text-slate-700">
              Measured precision
            </h3>
            <span className="text-[11px] text-slate-600">
              {precision.labelled} of {precision.total_findings} reviewed
            </span>
          </div>
          <p className="mt-1 text-[11px] text-slate-600">
            {precision.overall_precision === null ? (
              <>
                Not yet measured. Answer <em>Yes</em> or <em>No</em> on any finding
                below and this becomes a number rather than an assumption — it is
                also what decides whether the statistical model keeps running.
              </>
            ) : (
              <>
                {(precision.overall_precision * 100).toFixed(0)}% of reviewed
                indicators were worth the time.
              </>
            )}{" "}
            {precision.note}
          </p>
        </div>
      )}

      {findings.length > 0 && (
        <div className="mt-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h3 className="text-xs font-semibold text-slate-700">
              Findings ({findings.length})
            </h3>
            <div className="flex flex-wrap gap-1">
              <button
                type="button"
                onClick={() => setSeverityFilter("ALL")}
                className={`rounded px-2 py-1 text-[11px] ${
                  severityFilter === "ALL"
                    ? "bg-slate-900 text-white"
                    : "border border-slate-300 text-slate-600 hover:bg-slate-50"
                }`}
              >
                all
              </button>
              {(["high", "medium", "low"] as AnomalySeverityName[])
                .filter((name) => counts[name])
                .map((name) => (
                  <button
                    key={name}
                    type="button"
                    onClick={() => setSeverityFilter(name)}
                    className={`rounded px-2 py-1 text-[11px] ${
                      severityFilter === name
                        ? "bg-slate-900 text-white"
                        : "border border-slate-300 text-slate-600 hover:bg-slate-50"
                    }`}
                  >
                    {SEVERITY_LABEL[name]} ({counts[name]})
                  </button>
                ))}
            </div>
          </div>

          <ul className="mt-2 space-y-2">
            {visible.map((finding) => (
              <li
                key={finding.id}
                className="rounded border border-slate-200 p-3 hover:bg-slate-50"
              >
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <button
                    type="button"
                    onClick={() =>
                      setExpanded(expanded === finding.id ? null : finding.id)
                    }
                    className="flex-1 text-left"
                  >
                    <span className="text-xs font-medium">{finding.title}</span>
                    <span
                      className={`ml-2 rounded px-1.5 py-0.5 text-[10px] ${
                        SEVERITY_STYLE[finding.severity]
                      }`}
                    >
                      {SEVERITY_LABEL[finding.severity]}
                    </span>
                    <span className="ml-2 font-mono text-[10px] text-slate-400">
                      {finding.rule_id}
                    </span>
                    {finding.is_false_positive === true && (
                      <span className="ml-2 rounded bg-slate-200 px-1.5 py-0.5 text-[10px] text-slate-700">
                        marked not useful
                      </span>
                    )}
                    {finding.is_false_positive === false && (
                      <span className="ml-2 rounded bg-emerald-100 px-1.5 py-0.5 text-[10px] text-emerald-800">
                        confirmed
                      </span>
                    )}
                  </button>
                </div>

                {/* Observed against baseline — the comparison is the argument. */}
                <div className="mt-1.5 grid gap-1 text-[11px] sm:grid-cols-2">
                  <p className="text-slate-800">
                    <span className="text-slate-500">Observed: </span>
                    {finding.observed_value ?? "—"}
                  </p>
                  <p className="text-slate-800">
                    <span className="text-slate-500">Baseline: </span>
                    {finding.baseline_value ?? "not recorded"}
                  </p>
                </div>

                <p className="mt-1.5 whitespace-pre-line text-[11px] text-slate-700">
                  {finding.explanation}
                </p>

                {/* Always visible. Hiding the only control that produces a
                    precision label behind "click the row to expand" meant nobody
                    found it, and the measurement stayed at 0 of 15 forever. */}
                <div className="mt-2 flex flex-wrap items-center gap-2 border-t border-slate-100 pt-2">
                  <span className="text-[11px] text-slate-500">
                    {finding.is_false_positive === null
                      ? "Was this worth your time?"
                      : "Your verdict:"}
                  </span>
                  <button
                    type="button"
                    onClick={() => judge(finding.id, false)}
                    aria-pressed={finding.is_false_positive === false}
                    className={`rounded border px-2 py-1 text-[11px] ${
                      finding.is_false_positive === false
                        ? "border-emerald-600 bg-emerald-600 text-white"
                        : "border-emerald-300 text-emerald-800 hover:bg-emerald-50"
                    }`}
                  >
                    Yes — worth reviewing
                  </button>
                  <button
                    type="button"
                    onClick={() => judge(finding.id, true)}
                    aria-pressed={finding.is_false_positive === true}
                    className={`rounded border px-2 py-1 text-[11px] ${
                      finding.is_false_positive === true
                        ? "border-slate-700 bg-slate-700 text-white"
                        : "border-slate-300 text-slate-700 hover:bg-slate-100"
                    }`}
                  >
                    No — false positive
                  </button>
                  <button
                    type="button"
                    onClick={() =>
                      setExpanded(expanded === finding.id ? null : finding.id)
                    }
                    className="text-[11px] text-sky-700 underline underline-offset-2"
                  >
                    {expanded === finding.id ? "Hide evidence" : "Show evidence"}
                  </button>
                </div>

                {expanded === finding.id && (
                  <div className="mt-2 space-y-1.5 border-t border-slate-100 pt-2">
                    <p className="text-[11px] text-slate-800">
                      <span className="font-medium">What to check: </span>
                      {finding.required_check}
                    </p>
                    {finding.caveats.length > 0 && (
                      <ul className="list-disc space-y-0.5 pl-4 text-[11px] text-slate-600">
                        {finding.caveats.map((caveat) => (
                          <li key={caveat}>{caveat}</li>
                        ))}
                      </ul>
                    )}
                    {finding.model_version && (
                      <p className="font-mono text-[10px] text-slate-500">
                        {finding.model_version}
                        {finding.model_score !== null &&
                          ` · score ${finding.model_score.toFixed(3)}`}
                      </p>
                    )}
                    {finding.related_records.length > 0 && (
                      <p className="font-mono text-[10px] text-slate-500">
                        {finding.related_records
                          .slice(0, 8)
                          .map((record) => `${record.type}:${record.id ?? record.name}`)
                          .join(" · ")}
                      </p>
                    )}
                    {finding.graph_path.length > 0 && (
                      <p className="text-[10px] text-slate-500">
                        path:{" "}
                        {finding.graph_path
                          .slice(0, 6)
                          .map((edge) => `${edge.source}→${edge.target}`)
                          .join(", ")}
                      </p>
                    )}
                  </div>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      {!run && findings.length === 0 && !busy && (
        <p className="mt-4 text-xs text-slate-500">
          Collect evidence, resolve identities, read contracts and verify revenue
          first — the scan measures anomalies against those results.
        </p>
      )}
    </section>
  );
}
