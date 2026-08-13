"use client";

/**
 * Everything this workspace can hand over, as files.
 *
 * Deliberately its own panel rather than a button tucked inside the report. The
 * moment a reviewer wants to *act* on a finding they stop reading the screen and
 * start needing the data somewhere else — in a spreadsheet, in an email, attached
 * to a question for the founder. A download that only exists once the review is
 * finished is a download nobody uses, so every table is available at any point,
 * including while items are still disputed or awaiting a decision. Withheld rows
 * are exported too, carrying the reason they were withheld.
 */

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";

type Artifact = { key: string; label: string; format: string };

/** What each file is for, in the reviewer's terms rather than the schema's. */
const PURPOSE: Record<string, string> = {
  report: "The whole position as one page — the file to email or print to PDF.",
  summary: "Claimed against proven, and every headline figure. Start here.",
  "revenue-items": "Every classified amount with its rule, evidence and status.",
  anomalies: "Each indicator with what was observed, the baseline and what to check.",
  "review-queue": "The decisions still open, and how the settled ones were settled.",
  contracts: "Extracted terms per contract, and which contracts could not be read.",
  customers: "Resolved customers with their spellings, tax IDs and domains.",
  reconciliation: "Per invoice: billed, allocated, outstanding, bank-confirmed.",
  evidence: "The raw payments and bank rows every figure above was built from.",
};

export function DownloadsPanel({
  workspaceId,
  refreshKey,
}: {
  workspaceId: string;
  refreshKey?: number;
}) {
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setArtifacts((await api.listDownloads(workspaceId)).artifacts);
    } catch {
      // The catalogue is a convenience; failing to fetch it should not remove the
      // panel, because the bundle button below does not depend on it.
    }
  }, [workspaceId]);

  useEffect(() => {
    load();
  }, [load, refreshKey]);

  async function grab(label: string, run: () => Promise<string>) {
    setBusy(label);
    setError(null);
    setSaved(null);
    try {
      // Naming the file that was actually written is the whole point: the previous
      // downloads landed under a blob identifier and nobody could find them.
      setSaved(await run());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Download failed");
    } finally {
      setBusy(null);
    }
  }

  return (
    <section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold">Downloads</h2>
          <p className="mt-0.5 max-w-2xl text-xs text-slate-600">
            The report and every table behind it. Amounts appear twice in each CSV —
            grouped for reading and as a plain decimal for re-parsing — and rows that
            were withheld are included, carrying the reason. Available at any point,
            including while decisions are still open.
          </p>
        </div>
        <button
          type="button"
          onClick={() => grab("bundle", () => api.downloadBundle(workspaceId))}
          disabled={busy !== null}
          className="rounded-md bg-slate-900 px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50"
        >
          {busy === "bundle" ? "Building…" : "Download everything (.zip)"}
        </button>
      </div>

      {saved && (
        <p className="mt-3 rounded-md bg-emerald-50 px-3 py-2 text-xs text-emerald-800">
          Saved <span className="font-mono">{saved}</span> to your downloads folder.
        </p>
      )}
      {error && (
        <p role="alert" className="mt-3 rounded-md bg-rose-50 px-3 py-2 text-xs text-rose-700">
          {error}
        </p>
      )}

      <div className="mt-4 grid gap-2 sm:grid-cols-2">
        {artifacts.map((artifact) => (
          <div
            key={artifact.key}
            className="flex items-start justify-between gap-3 rounded-md border border-slate-200 p-3"
          >
            <div className="min-w-0">
              <p className="text-xs font-medium">
                {artifact.label}{" "}
                <span className="font-normal uppercase text-slate-400">
                  .{artifact.format}
                </span>
              </p>
              <p className="mt-0.5 text-[11px] text-slate-600">
                {PURPOSE[artifact.key] ?? ""}
              </p>
            </div>
            <button
              type="button"
              onClick={() =>
                grab(artifact.key, () =>
                  api.downloadArtifact(workspaceId, artifact.key),
                )
              }
              disabled={busy !== null}
              className="shrink-0 rounded-md border border-slate-300 px-2.5 py-1 text-[11px] font-medium disabled:opacity-50"
            >
              {busy === artifact.key ? "…" : "Download"}
            </button>
          </div>
        ))}
      </div>
    </section>
  );
}
