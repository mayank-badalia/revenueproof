"use client";

/**
 * Workspace dashboard.
 *
 * Currently shows the claim under test, evidence inventory, connection health and
 * the live processing trace. The revenue cards, waterfall, evidence graph and
 * review queue mount here as Features 4–8 land.
 */

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { ApiError, api } from "@/lib/api";
import type { AuditResponse, WorkspaceSummary } from "@/lib/types";
import { TraceViewer } from "@/components/TraceViewer";
import { ServiceStatus } from "@/components/ServiceStatus";
import { EvidencePanel } from "@/components/EvidencePanel";
import { IdentityPanel } from "@/components/IdentityPanel";
import { ContractsPanel } from "@/components/ContractsPanel";
import { ReconciliationPanel } from "@/components/ReconciliationPanel";
import { RevenuePanel } from "@/components/RevenuePanel";
import { AnomalyPanel } from "@/components/AnomalyPanel";
import { CriticPanel } from "@/components/CriticPanel";
import { DiligenceRoom } from "@/components/DiligenceRoom";
import { ReviewPanel } from "@/components/ReviewPanel";
import { DataSourcePanel } from "@/components/DataSourcePanel";
import { DownloadsPanel } from "@/components/DownloadsPanel";
import { RunPanel } from "@/components/RunPanel";

const EVIDENCE_LABELS: Record<string, string> = {
  raw_records: "Raw records",
  customers: "Customers",
  contracts: "Contracts",
  invoices: "Invoices",
  payments: "Payments",
  refunds: "Refunds",
  bank_transactions: "Bank transactions",
};

export default function WorkspacePage() {
  const params = useParams<{ id: string }>();
  const workspaceId = params.id;

  const [summary, setSummary] = useState<WorkspaceSummary | null>(null);
  const [audit, setAudit] = useState<AuditResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Bumped whenever any panel changes server state. Sibling panels observe it so
  // they re-fetch: collecting evidence creates contracts and customers, and those
  // panels would otherwise keep showing their empty state until a manual reload.
  const [dataVersion, setDataVersion] = useState(0);

  const load = useCallback(async () => {
    try {
      const [summaryData, auditData] = await Promise.all([
        api.workspaceSummary(workspaceId),
        api.auditLog(workspaceId).catch(() => null),
      ]);
      setSummary(summaryData);
      setAudit(auditData);
      setError(null);
      setDataVersion((v) => v + 1);
    } catch (err) {
      setError(
        err instanceof ApiError && err.status === 404
          ? "Workspace not found, or you do not have access to it."
          : err instanceof Error
            ? err.message
            : "Failed to load workspace",
      );
    }
  }, [workspaceId]);

  useEffect(() => {
    load();
  }, [load]);

  if (error) {
    return (
      <main className="mx-auto max-w-5xl px-4 py-8">
        <Link href="/" className="text-sm text-slate-600 hover:text-slate-900">
          ← All workspaces
        </Link>
        <p role="alert" className="mt-4 rounded-md bg-rose-50 px-4 py-3 text-sm text-rose-700">
          {error}
        </p>
      </main>
    );
  }

  if (!summary) {
    return (
      <main className="mx-auto max-w-5xl px-4 py-8">
        <p className="text-sm text-slate-500">Loading…</p>
      </main>
    );
  }

  const { workspace } = summary;

  return (
    <main className="mx-auto max-w-5xl px-4 py-8">
      <Link href="/" className="text-sm text-slate-600 hover:text-slate-900">
        ← All workspaces
      </Link>

      <header className="mt-3 mb-6">
        <h1 className="text-2xl font-semibold">{workspace.company_name}</h1>
        <p className="mt-1 text-sm text-slate-600">
          {workspace.reporting_period_start} → {workspace.reporting_period_end} ·{" "}
          {workspace.base_currency} · {workspace.accounting_method} · policy{" "}
          {workspace.active_policy_version}
        </p>
      </header>

      <div className="mb-6">
        <ServiceStatus />
      </div>

      {/* The claim under test, shown before any verified figure exists. */}
      <section className="mb-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <div className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
          <p className="text-xs text-slate-500">Claimed revenue</p>
          <p className="mt-1 text-xl font-semibold tabular-nums">
            {workspace.claimed_revenue.display}
          </p>
        </div>
        <div className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
          <p className="text-xs text-slate-500">Claimed ARR</p>
          <p className="mt-1 text-xl font-semibold tabular-nums">
            {workspace.claimed_arr.display}
          </p>
        </div>
        <div className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
          <p className="text-xs text-slate-500">Awaiting review</p>
          <p className="mt-1 text-xl font-semibold tabular-nums">
            {summary.open_review_items}
          </p>
        </div>
        <div className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
          <p className="text-xs text-slate-500">Quarantined records</p>
          <p
            className={`mt-1 text-xl font-semibold tabular-nums ${
              summary.quarantined_records > 0 ? "text-amber-700" : ""
            }`}
          >
            {summary.quarantined_records}
          </p>
        </div>
      </section>

      <section className="mb-6 rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
        <h2 className="text-sm font-semibold">Evidence collected</h2>
        <dl className="mt-3 grid grid-cols-2 gap-4 sm:grid-cols-4 lg:grid-cols-7">
          {Object.entries(EVIDENCE_LABELS).map(([key, label]) => (
            <div key={key}>
              <dt className="text-xs text-slate-500">{label}</dt>
              <dd className="mt-0.5 text-lg font-medium tabular-nums">
                {summary.evidence_counts[key] ?? 0}
              </dd>
            </div>
          ))}
        </dl>
        {Object.values(summary.evidence_counts).every((n) => n === 0) && (
          <p className="mt-3 rounded-md bg-slate-50 px-3 py-2 text-xs text-slate-600">
            No evidence yet. Connect a source or load the synthetic dataset to begin.
          </p>
        )}
      </section>

      <section className="mb-6 rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
        <h2 className="text-sm font-semibold">Connected sources</h2>
        {summary.connections.length === 0 ? (
          <p className="mt-3 text-sm text-slate-500">No sources connected yet.</p>
        ) : (
          <ul className="mt-3 space-y-2">
            {summary.connections.map((connection) => (
              <li
                key={connection.source_system}
                className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-slate-200 px-3 py-2 text-sm"
              >
                <span className="flex items-center gap-2">
                  <span
                    className={`h-2 w-2 rounded-full ${
                      connection.is_active ? "bg-emerald-500" : "bg-slate-300"
                    }`}
                  />
                  <span className="font-medium">{connection.source_system}</span>
                  {connection.is_synthetic && (
                    <span className="rounded bg-amber-100 px-1.5 py-0.5 text-xs text-amber-800">
                      synthetic
                    </span>
                  )}
                  {connection.is_test_mode && !connection.is_synthetic && (
                    <span className="rounded bg-sky-100 px-1.5 py-0.5 text-xs text-sky-800">
                      test mode
                    </span>
                  )}
                </span>
                <span className="text-xs text-slate-500">
                  {connection.records_imported} records
                  {connection.last_sync_at && ` · synced ${connection.last_sync_at.slice(0, 19)}`}
                  {connection.last_sync_error && (
                    <span className="text-rose-600"> · {connection.last_sync_error}</span>
                  )}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <div className="mb-6">
        <DataSourcePanel
          workspaceId={workspaceId}
          companyName={summary?.workspace.company_name ?? "workspace"}
          connections={summary?.connections ?? []}
          deploymentProviders={summary?.deployment_providers}
          onChanged={load}
        />
      </div>

      {/* Immediately after the source chooser: pick where the evidence comes from,
          then run. "No evidence" sends the reader back up to the chooser rather
          than describing it. */}
      <div className="mb-6">
        <RunPanel
          workspaceId={workspaceId}
          refreshKey={dataVersion}
          onChanged={load}
          onNeedsData={() =>
            document
              .getElementById("evidence-sources")
              ?.scrollIntoView({ behavior: "smooth", block: "center" })
          }
        />
      </div>

      <div className="mb-6">
        <EvidencePanel
          workspaceId={workspaceId}
          refreshKey={dataVersion}
          onChanged={load}
        />
      </div>

      <div className="mb-6">
        <IdentityPanel
          workspaceId={workspaceId}
          refreshKey={dataVersion}
          onChanged={load}
        />
      </div>

      <div className="mb-6">
        <ContractsPanel
          workspaceId={workspaceId}
          refreshKey={dataVersion}
          onChanged={load}
        />
      </div>

      <div className="mb-6">
        <ReconciliationPanel
          workspaceId={workspaceId}
          refreshKey={dataVersion}
          onChanged={load}
        />
      </div>

      <div className="mb-6">
        <RevenuePanel
          workspaceId={workspaceId}
          refreshKey={dataVersion}
          onChanged={load}
        />
      </div>

      <div className="mb-6">
        <AnomalyPanel
          workspaceId={workspaceId}
          refreshKey={dataVersion}
          onChanged={load}
        />
      </div>

      <div className="mb-6">
        <CriticPanel
          workspaceId={workspaceId}
          refreshKey={dataVersion}
          onChanged={load}
        />
      </div>

      <div className="mb-6">
        <ReviewPanel
          workspaceId={workspaceId}
          refreshKey={dataVersion}
          onChanged={load}
        />
      </div>

      <div className="mb-6">
        <DiligenceRoom
          workspaceId={workspaceId}
          companyName={summary?.workspace.company_name}
          refreshKey={dataVersion}
          onChanged={load}
        />
      </div>

      <div className="mb-6">
        <DownloadsPanel workspaceId={workspaceId} refreshKey={dataVersion} />
      </div>

      <div className="mb-6">
        <TraceViewer workspaceId={workspaceId} />
      </div>

      {audit && (
        <section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold">Audit log</h2>
            <span
              className={`rounded-full px-2 py-0.5 text-xs font-medium ${
                audit.integrity.valid
                  ? "bg-emerald-100 text-emerald-800"
                  : "bg-rose-100 text-rose-800"
              }`}
            >
              {audit.integrity.valid
                ? `hash chain verified (${audit.integrity.checked})`
                : `chain broken: ${audit.integrity.error}`}
            </span>
          </div>
          {/* Scrollable, and every event is here rather than the most recent
              fifteen. The audit log's whole claim is that the chain is complete;
              truncating it at an arbitrary point undercuts the thing it exists to
              demonstrate, and a reviewer following a figure back needs the entry
              from the run that produced it, not from the last one. */}
          <p className="mt-2 text-xs text-slate-500">
            {audit.events.length} event{audit.events.length === 1 ? "" : "s"}, newest
            first. Scroll for the full chain.
          </p>
          <div className="mt-2 max-h-96 overflow-auto rounded-md border border-slate-200">
            <table className="w-full text-left text-sm">
              <thead className="sticky top-0 bg-slate-50 text-xs text-slate-500">
                <tr>
                  <th className="py-1.5 pl-3 pr-3 font-medium">#</th>
                  <th className="py-1.5 pr-3 font-medium">Action</th>
                  <th className="py-1.5 pr-3 font-medium">Object</th>
                  <th className="py-1.5 pr-3 font-medium">Actor</th>
                  <th className="py-1.5 pr-3 font-medium">When</th>
                </tr>
              </thead>
              <tbody>
                {audit.events.map((entry) => (
                  <tr key={entry.sequence} className="border-t border-slate-100">
                    <td className="py-1.5 pl-3 pr-3 tabular-nums text-slate-500">
                      {entry.sequence}
                    </td>
                    <td className="py-1.5 pr-3 font-medium">{entry.action}</td>
                    <td className="py-1.5 pr-3 text-slate-600">{entry.object_type}</td>
                    <td className="py-1.5 pr-3 text-slate-600">{entry.actor.split(":")[0]}</td>
                    <td className="py-1.5 text-xs text-slate-500">
                      {entry.timestamp.slice(0, 19).replace("T", " ")}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </main>
  );
}
