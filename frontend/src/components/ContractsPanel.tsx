"use client";

/**
 * Feature 3 UI — extracted contract terms and the citations behind them.
 *
 * The recurring/one-time split is shown as two separate columns rather than one
 * contract value, because conflating them is the specific overstatement this
 * feature exists to catch: a ₹18,00,000 contract is not ₹18,00,000 of ARR when
 * ₹15,00,000 of it is a non-recurring implementation fee.
 *
 * Citations open inline with a verified/unverified badge. An unverified citation is
 * shown rather than hidden — the value resting on it was discarded, and a reviewer
 * needs to know that happened.
 */

import React, { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { ContractCitation, ContractRow, ContractRun } from "@/lib/types";

export function ContractsPanel({
  workspaceId,
  refreshKey,
  onChanged,
}: {
  workspaceId: string;
  /** Increments when a sibling panel changes server state. */
  refreshKey?: number;
  onChanged?: () => void;
}) {
  const [contracts, setContracts] = useState<ContractRow[]>([]);
  const [lastRun, setLastRun] = useState<ContractRun | null>(null);
  const [citations, setCitations] = useState<Record<string, ContractCitation[]>>({});
  const [expanded, setExpanded] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setContracts((await api.listContracts(workspaceId)).contracts);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load contracts");
    }
  }, [workspaceId]);

  useEffect(() => {
    load();
    // refreshKey re-runs this when another panel has changed server state.
  }, [load, refreshKey]);

  async function process() {
    setBusy(true);
    setError(null);
    try {
      setLastRun(await api.processContracts(workspaceId));
      await load();
      onChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Contract processing failed");
    } finally {
      setBusy(false);
    }
  }

  async function toggle(contract: ContractRow) {
    if (expanded === contract.id) {
      setExpanded(null);
      return;
    }
    setExpanded(contract.id);
    if (!citations[contract.id]) {
      try {
        const response = await api.contractCitations(workspaceId, contract.id);
        setCitations((current) => ({ ...current, [contract.id]: response.citations }));
      } catch {
        setCitations((current) => ({ ...current, [contract.id]: [] }));
      }
    }
  }

  // A contract is only "extracted" once terms were actually read from it.
  const isExtracted = (c: ContractRow) =>
    c.start_date !== null || c.recurring_amount.minor > 0;
  const extracted = contracts.filter(isExtracted).length;

  return (
    <section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold">Contract terms</h2>
          <p className="mt-0.5 text-xs text-slate-600">
            Reads each contract and separates recurring subscription value from
            one-time fees and future-period amounts. Every value carries a verified
            page citation.
          </p>
        </div>
        <button
          type="button"
          onClick={process}
          disabled={busy}
          className="rounded-md bg-slate-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-slate-800 disabled:opacity-50"
        >
          {busy ? "Reading contracts…" : "Read contracts"}
        </button>
      </div>

      {busy && (
        <p className="mt-3 rounded-md bg-sky-50 px-3 py-2 text-xs text-sky-800">
          Extraction is paced to the LLM provider&apos;s free-tier token budget, so a
          full set of contracts takes several minutes. Progress appears in the trace
          below as each document completes.
        </p>
      )}

      {error && (
        <p role="alert" className="mt-3 rounded-md bg-rose-50 px-3 py-2 text-xs text-rose-700">
          {error}
        </p>
      )}

      {lastRun && (
        <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
          {[
            ["Processed", lastRun.processed],
            ["Extracted", lastRun.extracted],
            ["Need review", lastRun.needs_review],
            ["Failed", lastRun.failed],
            ["Required OCR", lastRun.ocr_used],
            ["Amendments", lastRun.amendments_resolved],
          ].map(([label, value]) => (
            <div key={String(label)} className="rounded-md border border-slate-200 px-2 py-1.5">
              <p className="text-[11px] text-slate-500">{label}</p>
              <p className="text-base font-semibold tabular-nums">{value}</p>
            </div>
          ))}
        </div>
      )}

      {lastRun && lastRun.failed > 0 && (
        <div className="mt-4 rounded-md border border-rose-200 bg-rose-50 p-3">
          <h3 className="text-xs font-semibold text-rose-900">
            {lastRun.failed} contract{lastRun.failed === 1 ? "" : "s"} could not be read
          </h3>
          <p className="mt-0.5 text-xs text-rose-800">
            Their terms are unknown, not zero. They are excluded from every revenue
            figure and queued for review. The most common cause is the LLM
            provider&apos;s free-tier token budget being exhausted — re-run in a few
            minutes, or raise the quota.
          </p>
          <ul className="mt-1.5 space-y-0.5">
            {lastRun.outcomes
              .filter((o) => o.error)
              .slice(0, 5)
              .map((o) => (
                <li key={o.document_name} className="text-[11px] text-rose-900">
                  <span className="font-medium">{o.document_name}</span> — {o.error}
                </li>
              ))}
          </ul>
        </div>
      )}

      {contracts.length === 0 ? (
        <p className="mt-4 text-xs text-slate-500">
          No contracts yet. Collect evidence first, then read the contracts.
        </p>
      ) : (
        <div className="mt-5">
          <p className="mb-2 text-xs text-slate-600">
            {extracted} of {contracts.length} contracts have extracted terms.
          </p>
          <div className="overflow-x-auto rounded border border-slate-200">
            <table className="w-full min-w-[760px] text-left text-xs">
              <thead className="bg-slate-50 text-slate-500">
                <tr>
                  <th className="px-2 py-1.5 font-medium">Document</th>
                  <th className="px-2 py-1.5 font-medium">Customer</th>
                  <th className="px-2 py-1.5 font-medium">Term</th>
                  <th className="px-2 py-1.5 text-right font-medium">Recurring</th>
                  <th className="px-2 py-1.5 text-right font-medium">One-time</th>
                  <th className="px-2 py-1.5 text-right font-medium">Future</th>
                  <th className="px-2 py-1.5 font-medium">Cites</th>
                </tr>
              </thead>
              <tbody>
                {contracts.map((contract) => (
                  // The key belongs on the fragment: it is the element being mapped,
                  // and React cannot track two sibling rows without it.
                  <React.Fragment key={contract.id}>
                    <tr
                      onClick={() => toggle(contract)}
                      className="cursor-pointer border-t border-slate-100 hover:bg-slate-50"
                    >
                      <td className="px-2 py-1.5">
                        <span className="font-medium">
                          {contract.document_name.replace(/\.pdf$/i, "")}
                        </span>
                        <span className="ml-1 space-x-1">
                          {contract.ocr_applied && (
                            <span className="rounded bg-violet-100 px-1 text-[10px] text-violet-800">
                              OCR
                            </span>
                          )}
                          {contract.is_amendment && (
                            <span className="rounded bg-sky-100 px-1 text-[10px] text-sky-800">
                              amendment
                            </span>
                          )}
                          {contract.needs_human_review && (
                            <span className="rounded bg-amber-100 px-1 text-[10px] text-amber-800">
                              review
                            </span>
                          )}
                        </span>
                      </td>
                      <td className="px-2 py-1.5 text-slate-600">
                        {contract.stated_customer_name ?? "—"}
                      </td>
                      <td className="px-2 py-1.5 text-slate-600">
                        {contract.start_date
                          ? `${contract.start_date} → ${contract.end_date ?? "?"}`
                          : "—"}
                        <span className="ml-1 text-slate-400">
                          {contract.billing_frequency !== "unknown"
                            ? contract.billing_frequency
                            : ""}
                        </span>
                      </td>
                      {/* An unread contract must never render as a contract worth
                          zero. "INR 0.00" and "not yet read" are entirely different
                          statements about a company's revenue, and showing the
                          former for the latter is the exact overstatement-by-
                          omission this product exists to prevent. */}
                      <td className="px-2 py-1.5 text-right tabular-nums">
                        {isExtracted(contract) ? (
                          contract.recurring_amount.display
                        ) : (
                          <span className="text-slate-400 italic">not read</span>
                        )}
                      </td>
                      <td className="px-2 py-1.5 text-right tabular-nums text-slate-600">
                        {!isExtracted(contract)
                          ? "—"
                          : contract.one_time_amount.minor > 0
                            ? contract.one_time_amount.display
                            : "—"}
                      </td>
                      <td className="px-2 py-1.5 text-right tabular-nums text-slate-600">
                        {!isExtracted(contract)
                          ? "—"
                          : contract.future_period_amount.minor > 0
                            ? contract.future_period_amount.display
                            : "—"}
                      </td>
                      <td className="px-2 py-1.5 tabular-nums">
                        {contract.extraction_confidence !== null
                          ? `${Math.round(contract.extraction_confidence * 100)}%`
                          : "—"}
                      </td>
                    </tr>

                    {expanded === contract.id && (
                      <tr key={`${contract.id}-detail`} className="border-t border-slate-100">
                        <td colSpan={7} className="bg-slate-50 px-3 py-2">
                          {contract.review_reasons.length > 0 && (
                            <div className="mb-2 rounded bg-amber-50 px-2 py-1.5 text-[11px] text-amber-900">
                              {contract.review_reasons.map((reason, i) => (
                                <p key={i}>{reason}</p>
                              ))}
                            </div>
                          )}
                          {contract.unknown_fields.length > 0 && (
                            <p className="mb-2 text-[11px] text-slate-600">
                              <span className="font-medium">Not stated in the contract:</span>{" "}
                              {contract.unknown_fields.join(", ")}
                            </p>
                          )}
                          <table className="w-full text-[11px]">
                            <tbody>
                              {(citations[contract.id] ?? []).map((citation, i) => (
                                <tr key={i} className="align-top">
                                  <td className="w-40 py-0.5 pr-2 font-medium">
                                    {citation.field_name}
                                  </td>
                                  <td className="w-24 py-0.5 pr-2 text-slate-600">
                                    {citation.field_value ?? "—"}
                                  </td>
                                  <td className="w-14 py-0.5 pr-2 text-slate-500">
                                    p.{citation.page_number}
                                  </td>
                                  <td className="w-20 py-0.5 pr-2">
                                    <span
                                      className={`rounded px-1 ${
                                        citation.verified
                                          ? "bg-emerald-100 text-emerald-800"
                                          : "bg-rose-100 text-rose-800"
                                      }`}
                                    >
                                      {citation.verified ? "verified" : "unverified"}
                                    </span>
                                  </td>
                                  <td className="py-0.5 italic text-slate-600">
                                    &ldquo;{citation.quote.slice(0, 150)}
                                    {citation.quote.length > 150 ? "…" : ""}&rdquo;
                                    {citation.verification_note && (
                                      <span className="ml-1 not-italic text-amber-700">
                                        ({citation.verification_note})
                                      </span>
                                    )}
                                  </td>
                                </tr>
                              ))}
                              {(citations[contract.id] ?? []).length === 0 && (
                                <tr>
                                  <td className="py-1 text-slate-500">
                                    No citations recorded for this contract.
                                  </td>
                                </tr>
                              )}
                            </tbody>
                          </table>
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </section>
  );
}
