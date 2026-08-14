"use client";

/**
 * One workspace: the verification canvas, its inspector and its execution strip.
 */

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { ApiError, api, getToken, setToken } from "@/lib/api";
import { AppShell } from "@/components/shell/AppShell";
import { WorkspaceCanvas } from "@/components/canvas/WorkspaceCanvas";
import { Banner, Spinner } from "@/components/ui/primitives";
import type { Workspace, WorkspaceSummary } from "@/lib/types";

export default function WorkspacePage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const workspaceId = params.id;

  const [summary, setSummary] = useState<WorkspaceSummary | null>(null);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [email, setEmail] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [me, list, detail] = await Promise.all([
        api.me(),
        api.listWorkspaces(),
        api.workspaceSummary(workspaceId),
      ]);
      setEmail(me.email);
      setWorkspaces(list);
      setSummary(detail);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) router.push("/");
      else setError(err instanceof ApiError ? err.message : "Could not open this workspace");
    }
  }, [workspaceId, router]);

  useEffect(() => {
    if (!getToken()) {
      router.push("/");
      return;
    }
    void load();
  }, [load, router]);

  const workspace = summary?.workspace;

  return (
    <AppShell
      workspaces={workspaces}
      activeWorkspaceId={workspaceId}
      email={email}
      onSignOut={() => {
        setToken(null);
        router.push("/");
      }}
      topBar={
        <header className="flex h-[52px] shrink-0 items-center gap-3 border-b border-navy-line bg-navy-800 px-4 text-white">
          <Link
            href="/"
            className="text-[13px] text-white/55 transition-colors hover:text-white"
          >
            Workspaces
          </Link>
          <span className="text-white/25">/</span>
          <span className="truncate text-[13.5px] font-medium">
            {workspace?.company_name ?? "Loading…"}
          </span>
          {workspace && (
            <span className="ml-2 hidden items-center gap-3 font-mono text-[11.5px] tabular-nums text-white/50 md:flex">
              <span>
                {workspace.reporting_period_start} → {workspace.reporting_period_end}
              </span>
              <span className="text-white/25">·</span>
              <span>Claim {workspace.claimed_revenue.display}</span>
            </span>
          )}
        </header>
      }
    >
      {error ? (
        <div className="mx-auto max-w-md pt-20">
          <Banner tone="error">{error}</Banner>
        </div>
      ) : !summary ? (
        <div className="grid h-full place-items-center text-ink-3">
          <Spinner />
        </div>
      ) : (
        <WorkspaceCanvas workspaceId={workspaceId} summary={summary} />
      )}
    </AppShell>
  );
}
