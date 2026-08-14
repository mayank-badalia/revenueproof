"use client";

/**
 * The audit trail, and a live check that it has not been altered.
 *
 * The log is append-only and SHA-256 hash-chained: each entry carries the hash of the
 * one before it, so changing any historical row breaks every hash after it. The verdict
 * at the top is that check run now, not a claim made once at write time — which is the
 * only version of the claim worth anything.
 */

import { useCallback, useEffect, useState } from "react";

import { api, ApiError } from "@/lib/api";
import { Banner, Card, Eyebrow, Spinner } from "@/components/ui/primitives";
import { WorkspaceChrome } from "@/components/shell/WorkspaceChrome";
import type { AuditResponse, Workspace } from "@/lib/types";

export default function AuditPage() {
  const [selected, setSelected] = useState<string | null>(null);

  return (
    <WorkspaceChrome
      title="Audit log"
      subtitle="Every decision and every agent action, hash-chained so tampering is detectable."
    >
      {(workspaces) => {
        const active = selected ?? workspaces[0]?.id ?? null;
        return workspaces.length === 0 ? (
          <Card className="px-6 py-12 text-center text-[13px] text-ink-2">
            No workspaces yet, so nothing has been recorded.
          </Card>
        ) : (
          <div>
            <div className="mb-3 flex flex-wrap gap-1.5">
              {workspaces.map((ws) => (
                <button
                  key={ws.id}
                  onClick={() => setSelected(ws.id)}
                  className={`rounded-[7px] px-3 py-1.5 text-[12.5px] transition-colors ${
                    active === ws.id
                      ? "bg-cobalt text-white"
                      : "bg-paper text-ink-2 ring-1 ring-line hover:bg-slate-soft"
                  }`}
                >
                  {ws.company_name}
                </button>
              ))}
            </div>
            {active && <AuditFor workspace={workspaces.find((w) => w.id === active)!} />}
          </div>
        );
      }}
    </WorkspaceChrome>
  );
}

function AuditFor({ workspace }: { workspace: Workspace }) {
  const [data, setData] = useState<AuditResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setData(null);
    setError(null);
    try {
      setData(await api.auditLog(workspace.id));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not read the audit log");
    }
  }, [workspace.id]);

  useEffect(() => {
    void load();
  }, [load]);

  if (error) return <Banner tone="error">{error}</Banner>;
  if (!data)
    return (
      <div className="grid place-items-center py-12 text-ink-3">
        <Spinner />
      </div>
    );

  const { integrity, events } = data;

  return (
    <div>
      <div className="mb-3">
        <Banner tone={integrity.valid ? "success" : "error"}>
          {integrity.valid ? (
            <>
              <strong className="font-semibold">Chain verified.</strong> {integrity.checked}{" "}
              entries checked just now; every hash matches the entry before it.
            </>
          ) : (
            <>
              <strong className="font-semibold">Chain broken.</strong>{" "}
              {integrity.error ?? "An entry does not match its predecessor."}
            </>
          )}
        </Banner>
      </div>

      <Eyebrow>{events.length} entries, newest first</Eyebrow>
      <Card className="mt-2 overflow-hidden">
        {events.length === 0 ? (
          <p className="px-4 py-8 text-center text-[12.5px] text-ink-2">
            Nothing recorded for this workspace yet.
          </p>
        ) : (
          <ol>
            {events.map((event) => (
              <li
                key={event.event_hash}
                className="border-b border-line-2 px-4 py-2.5 last:border-0"
              >
                <div className="flex items-baseline justify-between gap-3">
                  <span className="flex min-w-0 items-baseline gap-2">
                    <span className="font-mono text-[11px] tabular-nums text-ink-3">
                      #{event.sequence}
                    </span>
                    <span className="truncate text-[12.5px] font-medium text-ink">
                      {event.action}
                    </span>
                  </span>
                  <span className="shrink-0 font-mono text-[11px] text-ink-3">
                    {new Date(event.timestamp).toLocaleString()}
                  </span>
                </div>
                <div className="mt-0.5 flex flex-wrap gap-x-3 text-[11.5px] text-ink-2">
                  <span>
                    <span className="text-ink-3">by</span> {event.actor}
                  </span>
                  <span>
                    <span className="text-ink-3">on</span> {event.object_type}
                  </span>
                  {event.reason && (
                    <span className="text-ink">
                      <span className="text-ink-3">reason</span> {event.reason}
                    </span>
                  )}
                </div>
                <div className="mt-0.5 truncate font-mono text-[10px] text-ink-3">
                  {event.event_hash}
                </div>
              </li>
            ))}
          </ol>
        )}
      </Card>
    </div>
  );
}
