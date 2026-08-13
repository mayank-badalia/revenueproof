"use client";

/**
 * Feature 1 UI — Connect Data (§10.2) and the evidence vault.
 *
 * Two things this panel deliberately makes visible rather than hiding:
 *
 * * **Provenance.** Every vaulted record shows its SHA-256 content hash and version.
 *   A reviewer can see that evidence was captured, not retyped.
 * * **Quarantine.** Records that failed validation are shown with their reason.
 *   A clean report over silently-dropped rows is exactly the failure mode the
 *   product exists to prevent, so rejected evidence is surfaced as a number the
 *   reviewer has to look at.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import type {
  EvidenceResponse,
  IngestionRun,
  QuarantineResponse,
} from "@/lib/types";

const SOURCE_LABELS: Record<string, string> = {
  razorpay: "Razorpay",
  zoho_books: "Zoho Books",
  google_drive: "Google Drive",
  hubspot: "HubSpot",
  bank_csv: "Bank statement",
};

function formatBytes(bytes: number | null): string {
  if (!bytes) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function EvidencePanel({
  workspaceId,
  refreshKey,
  onChanged,
}: {
  workspaceId: string;
  /** Increments when a sibling panel changes server state. */
  refreshKey?: number;
  onChanged?: () => void;
}) {
  const [evidence, setEvidence] = useState<EvidenceResponse | null>(null);
  const [quarantine, setQuarantine] = useState<QuarantineResponse | null>(null);
  const [lastRun, setLastRun] = useState<IngestionRun | null>(null);
  const [busy, setBusy] = useState(false);
  // Demonstration data is opt-in and off by default: once a provider key exists,
  // silently serving invented records under a "live" badge would be the worst of
  // both worlds. The choice is the operator's, and it is visible on the page.
  const [useDemoData, setUseDemoData] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    try {
      const [ev, q] = await Promise.all([
        api.listEvidence(workspaceId),
        api.quarantine(workspaceId),
      ]);
      setEvidence(ev);
      setQuarantine(q);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load evidence");
    }
  }, [workspaceId]);

  useEffect(() => {
    load();
    // refreshKey re-runs this when another panel has changed server state.
  }, [load, refreshKey]);

  async function runIngestion() {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const run = await api.runIngestion(workspaceId, useDemoData);
      if (run.error) {
        setError(run.error);
      } else {
        setLastRun(run);
        setNotice(
          `Collected ${run.total_canonical} canonical records across ${Object.keys(run.sources).length} sources.`,
        );
      }
      await load();
      onChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Ingestion failed");
    } finally {
      setBusy(false);
    }
  }

  async function uploadCsv(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    setUploading(true);
    setError(null);
    setNotice(null);
    try {
      const stats = await api.uploadBankCsv(workspaceId, file);
      setNotice(
        `Imported ${stats.canonical_written} transactions from ${file.name}` +
          (stats.quarantined > 0 ? ` — ${stats.quarantined} rows rejected.` : "."),
      );
      await load();
      onChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setUploading(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  }

  const totalEvidence =
    evidence?.counts.reduce((sum, row) => sum + row.count, 0) ?? 0;

  return (
    <section id="evidence-vault" className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold">Evidence vault</h2>
          <p className="mt-0.5 text-xs text-slate-600">
            Collect from connected sources. Every record is hashed and versioned at
            capture.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <label className="flex cursor-pointer items-center gap-1.5 text-xs text-slate-600">
            <input
              type="checkbox"
              checked={useDemoData}
              onChange={(event) => setUseDemoData(event.target.checked)}
              className="h-3.5 w-3.5"
            />
            demonstration data
          </label>
          <a
            href={`${process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000"}/api/v1/bank-csv/template`}
            className="rounded-md border border-slate-300 px-3 py-1.5 text-xs hover:bg-slate-50"
          >
            CSV template
          </a>
          <label className="cursor-pointer rounded-md border border-slate-300 px-3 py-1.5 text-xs hover:bg-slate-50">
            {uploading ? "Uploading…" : "Upload bank CSV"}
            <input
              ref={fileInput}
              type="file"
              accept=".csv,.txt"
              onChange={uploadCsv}
              disabled={uploading}
              className="hidden"
            />
          </label>
          <button
            type="button"
            onClick={runIngestion}
            disabled={busy}
            className="rounded-md bg-slate-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-slate-800 disabled:opacity-50"
          >
            {busy ? "Collecting…" : "Collect evidence"}
          </button>
        </div>
      </div>

      {error && (
        <p role="alert" className="mt-3 rounded-md bg-rose-50 px-3 py-2 text-xs text-rose-700">
          {error}
        </p>
      )}
      {notice && (
        <p className="mt-3 rounded-md bg-emerald-50 px-3 py-2 text-xs text-emerald-800">
          {notice}
        </p>
      )}

      {/* Per-source outcome of the last run */}
      {lastRun && (
        <div className="mt-4 overflow-x-auto">
          <table className="w-full min-w-[560px] text-left text-xs">
            <thead className="text-slate-500">
              <tr>
                <th className="py-1 pr-3 font-medium">Source</th>
                <th className="py-1 pr-3 font-medium">Mode</th>
                <th className="py-1 pr-3 text-right font-medium">Fetched</th>
                <th className="py-1 pr-3 text-right font-medium">New</th>
                <th className="py-1 pr-3 text-right font-medium">Duplicates</th>
                <th className="py-1 text-right font-medium">Quarantined</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(lastRun.sources).map(([source, stats]) => (
                <tr key={source} className="border-t border-slate-100">
                  <td className="py-1.5 pr-3 font-medium">
                    {SOURCE_LABELS[source] ?? source}
                  </td>
                  <td className="py-1.5 pr-3">
                    {stats.is_synthetic ? (
                      <span className="rounded bg-amber-100 px-1.5 py-0.5 text-amber-800">
                        synthetic
                      </span>
                    ) : (
                      <span className="rounded bg-emerald-100 px-1.5 py-0.5 text-emerald-800">
                        live
                      </span>
                    )}
                  </td>
                  <td className="py-1.5 pr-3 text-right tabular-nums">{stats.fetched}</td>
                  <td className="py-1.5 pr-3 text-right tabular-nums">
                    {stats.canonical_written}
                  </td>
                  <td className="py-1.5 pr-3 text-right tabular-nums text-slate-500">
                    {stats.duplicates}
                  </td>
                  <td
                    className={`py-1.5 text-right tabular-nums ${
                      stats.quarantined > 0 ? "text-amber-700" : "text-slate-500"
                    }`}
                  >
                    {stats.quarantined}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Vault inventory */}
      {evidence && evidence.counts.length > 0 && (
        <div className="mt-5">
          <h3 className="text-xs font-semibold text-slate-700">
            Vaulted evidence ({totalEvidence} records)
          </h3>
          <div className="mt-2 flex flex-wrap gap-2">
            {evidence.counts.map((row) => (
              <span
                key={`${row.source_system}-${row.record_type}`}
                className="rounded-md border border-slate-200 bg-slate-50 px-2 py-1 text-xs"
              >
                <span className="font-medium">{row.count}</span>{" "}
                {row.record_type.replace(/_/g, " ")}
                <span className="text-slate-500">
                  {" "}
                  · {SOURCE_LABELS[row.source_system] ?? row.source_system}
                </span>
              </span>
            ))}
          </div>

          <details className="mt-3">
            <summary className="cursor-pointer text-xs text-slate-600 hover:text-slate-900">
              Show provenance hashes
            </summary>
            <div className="mt-2 max-h-64 overflow-auto rounded border border-slate-200">
              <table className="w-full min-w-[620px] text-left text-xs">
                <thead className="sticky top-0 bg-slate-50 text-slate-500">
                  <tr>
                    <th className="px-2 py-1 font-medium">Source ID</th>
                    <th className="px-2 py-1 font-medium">Type</th>
                    <th className="px-2 py-1 font-medium">Content hash</th>
                    <th className="px-2 py-1 font-medium">Ver</th>
                    <th className="px-2 py-1 font-medium">File</th>
                  </tr>
                </thead>
                <tbody className="font-mono">
                  {evidence.records.map((record) => (
                    <tr key={record.id} className="border-t border-slate-100">
                      <td className="px-2 py-1">{record.source_id}</td>
                      <td className="px-2 py-1 text-slate-600">
                        {record.record_type}
                      </td>
                      <td className="px-2 py-1 text-slate-500">
                        {record.content_hash.slice(0, 16)}…
                      </td>
                      <td className="px-2 py-1 tabular-nums">{record.version}</td>
                      <td className="px-2 py-1 text-slate-500">
                        {record.has_file ? formatBytes(record.file_size_bytes) : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
        </div>
      )}

      {/* Quarantine — surfaced, never hidden */}
      {quarantine && quarantine.summary.total > 0 && (
        <div className="mt-5 rounded-md border border-amber-200 bg-amber-50 p-3">
          <h3 className="text-xs font-semibold text-amber-900">
            {quarantine.summary.total} records quarantined
          </h3>
          <p className="mt-0.5 text-xs text-amber-800">
            These failed validation and were excluded from every downstream
            calculation. They are shown here rather than dropped.
          </p>
          <ul className="mt-2 space-y-1">
            {quarantine.records.slice(0, 8).map((record) => (
              <li key={record.id} className="text-xs text-amber-900">
                <span className="font-mono">{record.source_id ?? "—"}</span>{" "}
                <span className="rounded bg-amber-200 px-1">{record.reason}</span>{" "}
                {record.detail}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
