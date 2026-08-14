"use client";

/**
 * Published positions and the files that carry them.
 *
 * A version is the point of the diligence room: the figures frozen at a moment, and a
 * diff against the one before it computed in code rather than narrated. This page is
 * where you take one away — as the report, as any single table, or as the whole bundle.
 */

import { useCallback, useEffect, useState } from "react";

import { api, ApiError } from "@/lib/api";
import { relativeTime } from "@/lib/format";
import { Banner, Button, Card, DownloadIcon, Eyebrow, Spinner } from "@/components/ui/primitives";
import { WorkspaceChrome } from "@/components/shell/WorkspaceChrome";
import type { ReportVersionRow, Workspace } from "@/lib/types";

export default function ReportsPage() {
  return (
    <WorkspaceChrome
      title="Reports"
      subtitle="Every published position, and every file this workspace can hand over."
    >
      {(workspaces) =>
        workspaces.length === 0 ? (
          <Card className="px-6 py-12 text-center text-[13px] text-ink-2">
            Nothing published yet. Run a verification and publish a version first.
          </Card>
        ) : (
          <div className="space-y-4">
            {workspaces.map((ws) => (
              <WorkspaceReports key={ws.id} workspace={ws} />
            ))}
          </div>
        )
      }
    </WorkspaceChrome>
  );
}

function WorkspaceReports({ workspace }: { workspace: Workspace }) {
  const [versions, setVersions] = useState<ReportVersionRow[] | null>(null);
  const [artifacts, setArtifacts] = useState<{ key: string; label: string; format: string }[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    const [v, d] = await Promise.allSettled([
      api.roomVersions(workspace.id),
      api.listDownloads(workspace.id),
    ]);
    setVersions(v.status === "fulfilled" ? v.value.versions : []);
    setArtifacts(d.status === "fulfilled" ? d.value.artifacts : []);
  }, [workspace.id]);

  useEffect(() => {
    void load();
  }, [load]);

  async function take(key: string, fn: () => Promise<unknown>) {
    setBusy(key);
    setError(null);
    try {
      await fn();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not build that file");
    } finally {
      setBusy(null);
    }
  }

  return (
    <Card className="overflow-hidden">
      <header className="flex items-center justify-between gap-3 border-b border-line px-4 py-3">
        <div className="min-w-0">
          <div className="truncate text-[13.5px] font-medium text-ink">
            {workspace.company_name}
          </div>
          <div className="mt-0.5 font-mono text-[11px] text-ink-3">
            {workspace.reporting_period_start} → {workspace.reporting_period_end} · claim{" "}
            {workspace.claimed_revenue.display}
          </div>
        </div>
        <div className="flex gap-2">
          <Button
            size="sm"
            disabled={busy !== null}
            onClick={() => void take("report", () => api.downloadReport(workspace.id))}
            icon={<DownloadIcon size={12} />}
          >
            Report
          </Button>
          <Button
            size="sm"
            variant="primary"
            disabled={busy !== null}
            onClick={() => void take("bundle", () => api.downloadBundle(workspace.id))}
            icon={busy === "bundle" ? <Spinner /> : <DownloadIcon size={12} />}
          >
            Everything
          </Button>
        </div>
      </header>

      {error && (
        <div className="px-4 pt-3">
          <Banner tone="error">{error}</Banner>
        </div>
      )}

      <div className="grid gap-4 px-4 py-3.5 md:grid-cols-2">
        <div>
          <Eyebrow>Published versions</Eyebrow>
          {versions === null ? (
            <Spinner className="mt-2 text-ink-3" />
          ) : versions.length === 0 ? (
            <p className="mt-2 text-[12.5px] text-ink-2">
              No version published yet. Run the chain and publish from the canvas.
            </p>
          ) : (
            <div className="mt-2 space-y-1.5">
              {versions.map((v) => (
                <div key={v.version} className="rounded-[7px] border border-line px-2.5 py-2">
                  <div className="flex items-baseline justify-between gap-2">
                    <span className="font-mono text-[12px] font-medium text-ink">
                      Version {v.version}
                    </span>
                    <span className="text-[11px] text-ink-3">
                      {relativeTime(v.published_at)}
                    </span>
                  </div>
                  <div className="mt-1 grid grid-cols-2 gap-x-3 gap-y-0.5 font-mono tnum text-[11px] text-ink-2">
                    <span>
                      <span className="text-ink-3">claimed</span> {v.claimed_revenue}
                    </span>
                    <span>
                      <span className="text-ink-3">recurring</span> {v.verified_recurring}
                    </span>
                    <span>
                      <span className="text-ink-3">one-time</span> {v.verified_one_time}
                    </span>
                    <span>
                      <span className="text-ink-3">refunded</span> {v.refunded_reversed}
                    </span>
                  </div>
                  {v.changes_from_previous?.length > 0 && (
                    <ul className="mt-1.5 space-y-0.5">
                      {v.changes_from_previous.map((c) => (
                        <li key={c.field} className="text-[11px] text-ink-2">
                          <span className="text-ink-3">{c.label}:</span> {c.before} → {c.after}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>

        <div>
          <Eyebrow>Tables</Eyebrow>
          <p className="mt-1 text-[11.5px] leading-relaxed text-ink-3">
            Each money column appears twice — grouped to read, and plain to re-parse.
            Withheld and disputed rows are included; the most useful export is the one
            containing what is still to decide.
          </p>
          <div className="mt-2 grid gap-1">
            {artifacts.map((a) => (
              <button
                key={a.key}
                disabled={busy !== null}
                onClick={() =>
                  void take(a.key, () => api.downloadArtifact(workspace.id, a.key))
                }
                className="flex items-center justify-between rounded-[6px] border border-line px-2.5 py-1.5 text-left transition-colors hover:border-cobalt/40 hover:bg-cobalt-soft/40 disabled:opacity-50"
              >
                <span className="text-[12px] text-ink">{a.label}</span>
                <span className="flex items-center gap-1.5 font-mono text-[10.5px] uppercase text-ink-3">
                  {a.format}
                  {busy === a.key ? <Spinner /> : <DownloadIcon size={11} />}
                </span>
              </button>
            ))}
          </div>
        </div>
      </div>
    </Card>
  );
}
