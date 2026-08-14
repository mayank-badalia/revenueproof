"use client";

/**
 * The dashboard answers one question on arrival: which claims are still open, and
 * how much of each is actually proven.
 *
 * The proven share is the only figure here that carries a bar, because it is the only
 * one where the *proportion* is the point. Counts are counts; a bar under a count is
 * decoration pretending to be information.
 */

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { api } from "@/lib/api";
import { relativeTime } from "@/lib/format";
import { Button, Card, Eyebrow, Spinner } from "@/components/ui/primitives";
import { CreateWorkspaceDialog } from "./CreateWorkspaceDialog";
import type { DiligenceRoomData, Workspace } from "@/lib/types";

interface Position {
  claimed: number;
  proven: number;
  pct: number | null;
  openDecisions: number;
  published: number;
  currency: string;
}

export function Dashboard({
  workspaces,
  email,
  onChanged,
}: {
  workspaces: Workspace[];
  email: string | null;
  onChanged: () => void;
}) {
  const router = useRouter();
  const [creating, setCreating] = useState(false);
  const [positions, setPositions] = useState<Record<string, Position>>({});
  const [loading, setLoading] = useState(true);

  /* The room is the only endpoint that knows what survived review, so the dashboard
     asks it per workspace rather than deriving a second, disagreeing figure here. */
  const loadPositions = useCallback(async () => {
    const next: Record<string, Position> = {};
    await Promise.all(
      workspaces.slice(0, 25).map(async (ws) => {
        try {
          const room = (await api.diligenceRoom(ws.id)) as DiligenceRoomData;
          const p = room.position;
          const claimed = p.claimed_revenue ?? 0;
          const proven = p.cash_received ?? 0;
          next[ws.id] = {
            claimed,
            proven,
            pct: claimed > 0 ? Math.round((proven / claimed) * 1000) / 10 : null,
            openDecisions: p.items_awaiting_review ?? 0,
            published: p.items_published ?? 0,
            currency: p.currency ?? ws.base_currency,
          };
        } catch {
          /* A workspace with nothing in it yet has no position. That is not an error;
             the row simply shows "not started". */
        }
      }),
    );
    setPositions(next);
    setLoading(false);
  }, [workspaces]);

  useEffect(() => {
    void loadPositions();
  }, [loadPositions]);

  const totals = Object.values(positions);
  const stats = [
    { label: "Workspaces", value: workspaces.length },
    {
      label: "Verified figures published",
      value: totals.reduce((sum, p) => sum + p.published, 0),
    },
    {
      label: "Decisions open",
      value: totals.reduce((sum, p) => sum + p.openDecisions, 0),
      tone: totals.some((p) => p.openDecisions > 0) ? "amber" : undefined,
    },
    {
      label: "Claims fully proven",
      value: totals.filter((p) => p.pct !== null && p.pct >= 95).length,
    },
  ];

  const hour = new Date().getHours();
  const greeting = hour < 12 ? "Good morning" : hour < 17 ? "Good afternoon" : "Good evening";
  const name = (email ?? "").split("@")[0].replace(/[._-]/g, " ");

  return (
    <div className="h-full overflow-y-auto scroll-thin">
      <div className="mx-auto max-w-[1160px] px-8 py-9">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between sm:gap-6">
          <div>
            <h1 className="text-[27px] font-semibold tracking-[-0.02em] text-ink">
              {greeting}
              {name ? `, ${name.split(" ")[0]}` : ""}
            </h1>
            <p className="mt-1 text-[13.5px] text-ink-2">
              Verify a revenue claim from evidence to bank receipt.
            </p>
          </div>
          <Button variant="primary" onClick={() => setCreating(true)} icon={<PlusIcon />}>
            Create workspace
          </Button>
        </div>

        <div className="mt-7 grid grid-cols-2 gap-3 sm:grid-cols-4">
          {stats.map((stat) => (
            <Card key={stat.label} className="px-4 py-3.5">
              <div className="text-[11.5px] text-ink-2">{stat.label}</div>
              <div
                className={`mt-1.5 font-mono tnum text-[26px] font-semibold leading-none tracking-[-0.02em] ${
                  stat.tone === "amber" ? "text-amber" : "text-ink"
                }`}
              >
                {loading && totals.length === 0 ? "—" : stat.value}
              </div>
            </Card>
          ))}
        </div>

        <div className="mt-7">
          <div className="mb-2.5 flex items-baseline justify-between">
            <Eyebrow>Workspaces</Eyebrow>
            {loading && <Spinner className="text-ink-3" />}
          </div>

          <Card className="overflow-x-auto">
            {workspaces.length === 0 ? (
              <div className="px-6 py-14 text-center">
                <p className="text-[14px] font-medium text-ink">No workspaces yet</p>
                <p className="mx-auto mt-1 max-w-[380px] text-[13px] leading-relaxed text-ink-2">
                  Create one to state a revenue claim, then load the records that should
                  support it.
                </p>
                <div className="mt-4 flex justify-center">
                  <Button variant="primary" onClick={() => setCreating(true)} icon={<PlusIcon />}>
                    Create workspace
                  </Button>
                </div>
              </div>
            ) : (
              <table className="w-full min-w-[720px] border-collapse text-left">
                <thead>
                  <tr className="border-b border-line bg-slate-soft/60">
                    {["Company", "Period", "Claim", "Proven", "Open", "Created"].map((h, i) => (
                      <th
                        key={h}
                        className={`px-4 py-2 text-[11px] font-semibold uppercase tracking-[0.06em] text-ink-3 ${
                          i >= 2 ? "text-right" : ""
                        }`}
                      >
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {workspaces.map((ws) => {
                    const p = positions[ws.id];
                    return (
                      <tr
                        key={ws.id}
                        onClick={() => router.push(`/workspaces/${ws.id}`)}
                        className="cursor-pointer border-b border-line-2 transition-colors last:border-b-0 hover:bg-cobalt-soft/50"
                      >
                        <td className="px-4 py-3">
                          <div className="flex items-center gap-2.5">
                            <span className="grid h-7 w-7 shrink-0 place-items-center rounded-[6px] bg-navy-800 font-mono text-[11px] font-semibold text-white">
                              {ws.company_name.slice(0, 1).toUpperCase()}
                            </span>
                            <span className="min-w-0">
                              <span className="block truncate text-[13.5px] font-medium text-ink">
                                {ws.company_name}
                              </span>
                              {ws.legal_name && (
                                <span className="block truncate text-[12px] text-ink-3">
                                  {ws.legal_name}
                                </span>
                              )}
                            </span>
                          </div>
                        </td>
                        <td className="whitespace-nowrap px-4 py-3 text-[12.5px] text-ink-2">
                          {ws.reporting_period_start} → {ws.reporting_period_end}
                        </td>
                        <td className="whitespace-nowrap px-4 py-3 text-right font-mono tnum text-[13px] text-ink">
                          {ws.claimed_revenue.display}
                        </td>
                        <td className="px-4 py-3 text-right">
                          {p?.pct === null || p === undefined ? (
                            <span className="text-[12.5px] text-ink-3">Not started</span>
                          ) : (
                            <div className="inline-flex w-[92px] flex-col items-end gap-1">
                              <span
                                className={`font-mono tnum text-[13px] font-medium ${
                                  p.pct >= 90
                                    ? "text-emerald"
                                    : p.pct >= 50
                                      ? "text-amber"
                                      : "text-rust"
                                }`}
                              >
                                {p.pct}%
                              </span>
                              <span className="h-[3px] w-full overflow-hidden rounded-full bg-line">
                                <span
                                  className={`block h-full rounded-full ${
                                    p.pct >= 90
                                      ? "bg-emerald"
                                      : p.pct >= 50
                                        ? "bg-amber"
                                        : "bg-rust"
                                  }`}
                                  style={{ width: `${Math.min(100, Math.max(2, p.pct))}%` }}
                                />
                              </span>
                            </div>
                          )}
                        </td>
                        <td className="px-4 py-3 text-right font-mono tnum text-[13px]">
                          {p ? (
                            p.openDecisions > 0 ? (
                              <span className="text-amber">{p.openDecisions}</span>
                            ) : (
                              <span className="text-ink-3">0</span>
                            )
                          ) : (
                            <span className="text-ink-3">—</span>
                          )}
                        </td>
                        <td className="px-4 py-3 text-right text-[12.5px] text-ink-3">
                          {relativeTime(ws.created_at)}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </Card>
        </div>
      </div>

      <CreateWorkspaceDialog
        open={creating}
        onClose={() => setCreating(false)}
        onCreated={(ws) => {
          setCreating(false);
          onChanged();
          router.push(`/workspaces/${ws.id}`);
        }}
      />
    </div>
  );
}

function PlusIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden>
      <path d="M8 3.2v9.6M3.2 8h9.6" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
    </svg>
  );
}
