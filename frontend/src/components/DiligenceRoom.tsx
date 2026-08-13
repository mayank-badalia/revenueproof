"use client";

/**
 * Feature 8 UI — the diligence room.
 *
 * This is the screen an outside reviewer actually reads, and it is built around one
 * claim: **every supported rupee is traceable.** Clicking any amount rebuilds the
 * chain that produced it — customer, contract clause, invoice, payment, bank credit,
 * refund — with the rule and the critic's verdict attached.
 *
 * Three choices worth stating:
 *
 * * **Withheld amounts are shown, with the reason.** A total you cannot check is
 *   worth less than a gap you can see, so anything the critic did not approve is
 *   listed beside the published figures rather than quietly dropped.
 * * **The chain is a list, not a canvas.** A force-directed graph looks impressive
 *   and is hard to read, impossible to print and unusable with a keyboard. The chain
 *   is short and strictly ordered, so an ordered list *is* the honest visualisation
 *   — which also satisfies the spec's requirement for a non-graph alternative.
 * * **Versions are immutable.** Publishing freezes the position; the history shows
 *   what moved between versions and by how much, computed in code.
 */

import { Fragment, useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { ChangeImpact, DiligenceRoomData, EvidenceTrace } from "@/lib/types";

const KIND_STYLE: Record<string, string> = {
  customer: "bg-slate-800",
  contract: "bg-indigo-600",
  invoice: "bg-sky-600",
  payment: "bg-emerald-600",
  bank: "bg-teal-700",
  refund: "bg-rose-600",
};

const KIND_LABEL: Record<string, string> = {
  customer: "Customer",
  contract: "Contract",
  invoice: "Invoice",
  payment: "Payment",
  bank: "Bank receipt",
  refund: "Refund",
};

export function DiligenceRoom({
  workspaceId,
  companyName,
  refreshKey,
  onChanged,
}: {
  workspaceId: string;
  companyName?: string;
  refreshKey?: number;
  onChanged?: () => void;
}) {
  const [room, setRoom] = useState<DiligenceRoomData | null>(null);
  const [trace, setTrace] = useState<EvidenceTrace | null>(null);
  const [tracing, setTracing] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showWithheld, setShowWithheld] = useState(false);
  const [impact, setImpact] = useState<ChangeImpact | null>(null);
  const [rerunning, setRerunning] = useState(false);

  const load = useCallback(async () => {
    try {
      const [next, changes] = await Promise.all([
        api.diligenceRoom(workspaceId),
        api.detectChanges(workspaceId).catch(() => null),
      ]);
      setRoom(next);
      setImpact(changes);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load the room");
    }
  }, [workspaceId]);

  async function rerun() {
    setRerunning(true);
    setError(null);
    try {
      await api.rerunAffected(workspaceId);
      await load();
      onChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "The rerun failed");
    } finally {
      setRerunning(false);
    }
  }

  useEffect(() => {
    load();
  }, [load, refreshKey]);

  async function openTrace(itemId: string) {
    if (tracing === itemId) {
      setTracing(null);
      setTrace(null);
      return;
    }
    setTracing(itemId);
    setTrace(null);
    try {
      setTrace(await api.traceEvidence(workspaceId, itemId));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not trace this amount");
    }
  }

  async function publish() {
    setBusy(true);
    setError(null);
    try {
      await api.publishVersion(workspaceId);
      await load();
      onChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not publish a version");
    } finally {
      setBusy(false);
    }
  }

  const position = room?.position;
  const visible = (room?.items ?? []).filter(
    (item) => showWithheld || item.is_published,
  );

  return (
    <section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold">Diligence room</h2>
          <p className="mt-0.5 max-w-2xl text-xs text-slate-600">
            The published position, the history of how it moved, and the evidence
            chain behind every figure. Click any amount to follow it from the
            customer to the bank credit that confirms it.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            onClick={() => api.downloadReport(workspaceId)}
            className="rounded-md border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50"
          >
            Download report
          </button>
          {impact && !impact.unchanged && (
            <button
              type="button"
              onClick={rerun}
              disabled={rerunning}
              className="rounded-md border border-amber-400 bg-amber-50 px-3 py-1.5 text-xs font-medium text-amber-900 hover:bg-amber-100 disabled:opacity-50"
            >
              {rerunning ? "Rerunning…" : "Rerun affected work"}
            </button>
          )}
          <button
            type="button"
            onClick={publish}
            disabled={busy}
            className="rounded-md bg-slate-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-slate-800 disabled:opacity-50"
          >
            {busy ? "Publishing…" : "Publish version"}
          </button>
        </div>
      </div>

      {error && (
        <p role="alert" className="mt-3 rounded-md bg-rose-50 px-3 py-2 text-xs text-rose-700">
          {error}
        </p>
      )}

      {position && (
        <>
          {/* The one comparison this product exists to make. Every other figure on
              this screen is a component of it, and a reader who has to add four
              cards together to learn whether the claim held has not been answered.
              Stated as arithmetic they can check, in both directions. */}
          <ClaimVerdict
            claimed={position.claimed_revenue}
            proven={position.verified_recurring + position.verified_one_time}
            currency={position.currency}
            withheld={position.items_total - position.items_published}
          />

          <div className="mt-4 grid gap-3 sm:grid-cols-4">
            <Metric
              label="Verified recurring"
              value={money(position.verified_recurring, position.currency)}
              tone="ok"
            />
            <Metric
              label="Verified one-time"
              value={money(position.verified_one_time, position.currency)}
              tone="ok"
            />
            <Metric
              label="Refunded / reversed"
              value={money(position.refunded_reversed, position.currency)}
            />
            <Metric
              label="Concentration"
              value={
                position.largest_customer_concentration_pct !== null
                  ? `${position.largest_customer_concentration_pct.toFixed(1)}%`
                  : "—"
              }
              note={
                position.hhi !== null
                  ? `HHI ${position.hhi.toFixed(0)} · of ${position.concentration_basis}, ` +
                    `${position.concentration_customers} customers`
                  : undefined
              }
            />
          </div>

          <p className="mt-3 rounded-md bg-slate-50 px-3 py-2 text-[11px] text-slate-600">
            {room?.caveat}
          </p>

          {/* Freshness. A position is only as current as the evidence under it, and
              "we looked and nothing moved" is a different statement from silence. */}
          {impact && (
            <div
              className={`mt-2 rounded-md px-3 py-2 text-[11px] ${
                impact.unchanged
                  ? "bg-slate-50 text-slate-600"
                  : "bg-amber-50 text-amber-900"
              }`}
            >
              <span className="font-medium">
                {impact.unchanged ? "Evidence unchanged." : "Evidence has moved."}
              </span>{" "}
              {impact.summary}
              {!impact.unchanged && impact.affected_customers.length > 0 && (
                <span>
                  {" "}
                  Affected: {impact.affected_customers.slice(0, 5).join(", ")}
                  {impact.affected_customers.length > 5 && " and others"}.
                </span>
              )}
              <span className="mt-1 block text-[10px] opacity-80">
                {impact.monitoring.note}
              </span>
            </div>
          )}

          <p className="mt-2 text-[11px] text-slate-600">
            <span className="font-medium">{room?.published_count} published</span> ·{" "}
            {room?.withheld_count} withheld pending review ·{" "}
            {position.items_awaiting_review} decisions open (over{" "}
            {position.review_records} records) ·{" "}
            {position.open_anomalies} open indicators
          </p>
        </>
      )}

      {/* --- version history ---------------------------------------------- */}
      {room && room.history.length > 0 && (
        <div className="mt-5">
          <h3 className="text-xs font-semibold text-slate-700">Version history</h3>
          <ul className="mt-2 space-y-1.5">
            {room.history.slice(0, 5).map((version) => (
              <li
                key={version.version}
                className="rounded border border-slate-200 px-3 py-2 text-[11px]"
              >
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <span className="font-medium">Version {version.version}</span>
                  <span className="text-slate-500">
                    {version.published_at?.slice(0, 19).replace("T", " ")} · policy{" "}
                    {version.policy_version}
                  </span>
                </div>
                <p className="mt-0.5 text-slate-700">{version.change_explanation}</p>
                {version.changes_from_previous.length > 0 && (
                  <ul className="mt-1 space-y-0.5 text-slate-600">
                    {version.changes_from_previous.slice(0, 4).map((change) => (
                      <li key={change.field}>
                        {change.label}: {change.before} → {change.after}{" "}
                        <span
                          className={
                            change.direction === "increased"
                              ? "text-emerald-700"
                              : "text-rose-700"
                          }
                        >
                          ({change.direction})
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* --- traceable amounts -------------------------------------------- */}
      {room && room.items.length > 0 && (
        <div className="mt-5">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h3 className="text-xs font-semibold text-slate-700">
              Traceable amounts ({visible.length})
            </h3>
            <label className="flex items-center gap-1.5 text-[11px] text-slate-600">
              <input
                type="checkbox"
                checked={showWithheld}
                onChange={(event) => setShowWithheld(event.target.checked)}
              />
              show withheld ({room.withheld_count})
            </label>
          </div>

          <div className="mt-2 max-h-[26rem] overflow-auto rounded border border-slate-200">
            <table className="w-full min-w-[640px] text-left text-xs">
              <thead className="sticky top-0 bg-slate-50 text-slate-500">
                <tr>
                  <th className="px-2 py-1.5 font-medium">Amount</th>
                  <th className="px-2 py-1.5 font-medium">Classification</th>
                  <th className="px-2 py-1.5 text-right font-medium">Recognised</th>
                  <th className="px-2 py-1.5 font-medium">Status</th>
                </tr>
              </thead>
              <tbody>
                {visible.map((item) => (
                  <Fragment key={item.id}>
                    <tr
                      onClick={() => openTrace(item.id)}
                      className="cursor-pointer border-t border-slate-100 hover:bg-slate-50"
                    >
                      <td className="px-2 py-1.5 font-medium">{item.description}</td>
                      <td className="px-2 py-1.5 text-[11px] text-slate-600">
                        {item.classification.replace(/_/g, " ").toLowerCase()}
                      </td>
                      <td className="px-2 py-1.5 text-right tabular-nums">
                        {item.recognized.minor > 0 ? item.recognized.display : "—"}
                      </td>
                      <td className="px-2 py-1.5">
                        {item.is_published ? (
                          <span className="rounded bg-emerald-100 px-1.5 py-0.5 text-[10px] text-emerald-800">
                            published
                          </span>
                        ) : (
                          <span
                            className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] text-amber-900"
                            title={item.withheld_because ?? undefined}
                          >
                            withheld
                          </span>
                        )}
                      </td>
                    </tr>
                    {tracing === item.id && (
                      <tr className="bg-slate-50">
                        <td colSpan={4} className="px-3 py-3">
                          {!item.is_published && item.withheld_because && (
                            <p className="mb-2 rounded bg-amber-50 px-2 py-1.5 text-[11px] text-amber-900">
                              <span className="font-medium">Withheld: </span>
                              {item.withheld_because}
                            </p>
                          )}
                          {trace ? <Chain trace={trace} /> : (
                            <p className="text-[11px] text-slate-500">Tracing…</p>
                          )}
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {!room && !error && (
        <p className="mt-4 text-xs text-slate-500">Loading the room…</p>
      )}
    </section>
  );
}

/** The evidence chain as an ordered list — printable, keyboard-navigable, honest. */
function Chain({ trace }: { trace: EvidenceTrace }) {
  return (
    <div>
      <div className="flex flex-wrap items-center gap-1.5">
        {trace.nodes.map((node, index) => (
          <span key={node.id} className="flex items-center gap-1.5">
            {index > 0 && <span className="text-slate-400">→</span>}
            <span
              className={`rounded px-1.5 py-0.5 text-[10px] text-white ${
                KIND_STYLE[node.kind] ?? "bg-slate-500"
              }`}
            >
              {KIND_LABEL[node.kind] ?? node.kind}
            </span>
          </span>
        ))}
        {trace.complete ? (
          <span className="ml-1 rounded bg-emerald-100 px-1.5 py-0.5 text-[10px] text-emerald-800">
            chain complete
          </span>
        ) : (
          <span className="ml-1 rounded bg-amber-100 px-1.5 py-0.5 text-[10px] text-amber-900">
            chain incomplete
          </span>
        )}
      </div>

      <ol className="mt-2 space-y-1.5">
        {trace.nodes.map((node) => (
          <li key={node.id} className="rounded border border-slate-200 bg-white p-2">
            <div className="flex flex-wrap items-baseline gap-2">
              <span className="text-[10px] uppercase tracking-wide text-slate-500">
                {KIND_LABEL[node.kind] ?? node.kind}
              </span>
              <span className="text-[11px] font-medium">{node.label}</span>
              {typeof node.amount === "object" && node.amount && (
                <span className="text-[11px] tabular-nums text-slate-700">
                  {(node.amount as { display: string }).display}
                </span>
              )}
              {typeof node.status === "string" && (
                <span className="text-[10px] text-slate-500">{node.status}</span>
              )}
            </div>
            {Array.isArray(node.quotes) && node.quotes.length > 0 && (
              <ul className="mt-1 space-y-0.5">
                {(node.quotes as { field: string; page: number; quote: string }[]).map(
                  (quote) => (
                    <li key={`${quote.field}-${quote.page}`} className="text-[10px] text-slate-600">
                      <span className="font-medium">
                        {quote.field} (p{quote.page}):
                      </span>{" "}
                      <span className="italic">“{quote.quote}”</span>
                    </li>
                  ),
                )}
              </ul>
            )}
            {typeof node.narration === "string" && node.narration && (
              <p className="mt-0.5 font-mono text-[10px] text-slate-500">
                {node.narration}
              </p>
            )}
          </li>
        ))}
      </ol>

      {trace.breaks.length > 0 && (
        <ul className="mt-2 space-y-0.5">
          {trace.breaks.map((reason) => (
            <li key={reason} className="text-[11px] text-amber-800">
              {reason}
            </li>
          ))}
        </ul>
      )}

      {trace.critic && (
        <p className="mt-2 text-[11px] text-slate-600">
          <span className="font-medium">Critic {trace.critic.verdict.toLowerCase()}</span>{" "}
          — {trace.critic.settled_by}. {trace.critic.reasoning}
        </p>
      )}
      <p className="mt-1 text-[11px] text-slate-600">
        <span className="font-medium">Rule {trace.rule_id}:</span>{" "}
        {trace.rule_explanation}
      </p>
    </div>
  );
}

/**
 * Claimed against proven, and the gap between them named honestly.
 *
 * The gap is *not* called an overstatement. Unreviewed evidence and disproven
 * evidence both land here, and the difference between them is the whole reason the
 * review queue exists — so the caption says which it is, and the number of withheld
 * items travels with it.
 */
function ClaimVerdict({
  claimed,
  proven,
  currency,
  withheld,
}: {
  claimed: number;
  proven: number;
  currency: string;
  withheld: number;
}) {
  const gap = claimed - proven;
  const pct = claimed > 0 ? (proven / claimed) * 100 : null;
  const over = gap < 0;
  return (
    <div className="mt-4 rounded-md border border-slate-300 bg-white p-4">
      <div className="flex flex-wrap items-baseline gap-x-6 gap-y-2">
        <div>
          <p className="text-[11px] uppercase tracking-wide text-slate-500">Claimed</p>
          <p className="mt-1 text-xl font-semibold tabular-nums">
            {money(claimed, currency)}
          </p>
        </div>
        <span aria-hidden className="text-lg text-slate-400">
          →
        </span>
        <div>
          <p className="text-[11px] uppercase tracking-wide text-slate-500">
            Proven and published
          </p>
          <p className="mt-1 text-xl font-semibold tabular-nums text-emerald-700">
            {money(proven, currency)}
          </p>
        </div>
        <div>
          <p className="text-[11px] uppercase tracking-wide text-slate-500">
            {over ? "Evidence beyond the claim" : "Not yet proven"}
          </p>
          <p
            className={`mt-1 text-xl font-semibold tabular-nums ${
              over ? "text-slate-700" : "text-amber-700"
            }`}
          >
            {money(Math.abs(gap), currency)}
          </p>
        </div>
        {pct !== null && (
          <div>
            <p className="text-[11px] uppercase tracking-wide text-slate-500">
              Share of claim proven
            </p>
            <p className="mt-1 text-xl font-semibold tabular-nums">{pct.toFixed(1)}%</p>
          </div>
        )}
      </div>
      <p className="mt-3 text-[11px] text-slate-600">
        {over
          ? "Evidence supports more than was claimed. The excess is listed below with its classification."
          : `${money(Math.abs(gap), currency)} of the claim is not published. That is not a finding of overstatement — ` +
            `${withheld} item${withheld === 1 ? "" : "s"} are withheld pending review, and each is listed below with the ` +
            `reason it has not been published.`}
      </p>
    </div>
  );
}

function Metric({
  label,
  value,
  note,
  tone,
}: {
  label: string;
  value: string;
  note?: string;
  tone?: "ok";
}) {
  return (
    <div className="rounded-md border border-slate-300 bg-slate-50 p-3">
      <p className="text-[11px] uppercase tracking-wide text-slate-500">{label}</p>
      <p
        className={`mt-1 text-lg font-semibold tabular-nums ${
          tone === "ok" ? "text-emerald-700" : ""
        }`}
      >
        {value}
      </p>
      {note && <p className="mt-1 text-[11px] text-slate-600">{note}</p>}
    </div>
  );
}

function money(minor: number, currency: string): string {
  // Display only — the backend sends preformatted strings everywhere a figure is
  // authoritative. This is the one place a raw minor-unit total arrives, and it is
  // integer division, never floating-point arithmetic on a money value.
  const whole = Math.trunc(minor / 100);
  const paise = Math.abs(minor % 100);
  return `${currency} ${whole.toLocaleString("en-IN")}.${String(paise).padStart(2, "0")}`;
}
