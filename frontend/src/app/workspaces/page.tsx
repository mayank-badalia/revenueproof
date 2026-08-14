"use client";

/** Every workspace, with what each one currently proves. */

import Link from "next/link";

import { relativeTime } from "@/lib/format";
import { Card } from "@/components/ui/primitives";
import { WorkspaceChrome } from "@/components/shell/WorkspaceChrome";

export default function WorkspacesPage() {
  return (
    <WorkspaceChrome
      title="Workspaces"
      subtitle="One claim each, for one reporting period, with the evidence that tests it."
    >
      {(workspaces) =>
        workspaces.length === 0 ? (
          <Card className="px-6 py-14 text-center">
            <p className="text-[14px] font-medium text-ink">No workspaces yet</p>
            <p className="mt-1 text-[13px] text-ink-2">
              Create one from the dashboard to state a revenue claim.
            </p>
            <Link
              href="/"
              className="mt-4 inline-block text-[13px] font-medium text-cobalt hover:underline"
            >
              Go to the dashboard
            </Link>
          </Card>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {workspaces.map((ws) => (
              <Link key={ws.id} href={`/workspaces/${ws.id}`}>
                <Card className="h-full px-4 py-3.5 transition-colors hover:border-cobalt/50 hover:bg-cobalt-soft/30">
                  <div className="flex items-start gap-2.5">
                    <span className="grid h-8 w-8 shrink-0 place-items-center rounded-[6px] bg-navy-800 font-mono text-[12px] font-semibold text-white">
                      {ws.company_name.slice(0, 1).toUpperCase()}
                    </span>
                    <div className="min-w-0">
                      <div className="truncate text-[13.5px] font-medium text-ink">
                        {ws.company_name}
                      </div>
                      <div className="mt-0.5 font-mono text-[11px] text-ink-3">
                        {ws.reporting_period_start} → {ws.reporting_period_end}
                      </div>
                    </div>
                  </div>
                  <dl className="mt-3 space-y-1">
                    <div className="flex justify-between">
                      <dt className="text-[12px] text-ink-2">Claimed revenue</dt>
                      <dd className="font-mono tnum text-[12px] text-ink">
                        {ws.claimed_revenue.display}
                      </dd>
                    </div>
                    <div className="flex justify-between">
                      <dt className="text-[12px] text-ink-2">Claimed ARR</dt>
                      <dd className="font-mono tnum text-[12px] text-ink">
                        {ws.claimed_arr.display}
                      </dd>
                    </div>
                    <div className="flex justify-between">
                      <dt className="text-[12px] text-ink-2">Created</dt>
                      <dd className="text-[12px] text-ink-3">{relativeTime(ws.created_at)}</dd>
                    </div>
                  </dl>
                </Card>
              </Link>
            ))}
          </div>
        )
      }
    </WorkspaceChrome>
  );
}
