"use client";

/**
 * The shared frame for every page that is not the canvas: sidebar, top bar, an
 * authentication check and a service-health indicator.
 *
 * The health dot exists because three of this product's four dependencies can be down
 * while the app still renders — and a page of empty tables looks identical to a page
 * of genuinely empty results. Saying which service is unreachable turns a mystery into
 * an instruction.
 */

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState, type ReactNode } from "react";

import { ApiError, api, getToken, setToken } from "@/lib/api";
import { AppShell } from "./AppShell";
import { Banner, Spinner } from "@/components/ui/primitives";
import type { HealthStatus, Workspace } from "@/lib/types";

export function WorkspaceChrome({
  title,
  subtitle,
  children,
  activeWorkspaceId,
}: {
  title: string;
  subtitle?: string;
  children: (workspaces: Workspace[]) => ReactNode;
  activeWorkspaceId?: string;
}) {
  const router = useRouter();
  const [workspaces, setWorkspaces] = useState<Workspace[] | null>(null);
  const [email, setEmail] = useState<string | null>(null);
  const [health, setHealth] = useState<HealthStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [me, list] = await Promise.all([api.me(), api.listWorkspaces()]);
      setEmail(me.email);
      setWorkspaces(list);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) router.push("/");
      else setError(err instanceof ApiError ? err.message : "Could not load your workspaces");
    }
    try {
      setHealth(await api.health());
    } catch {
      setHealth(null);
    }
  }, [router]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      if (!getToken()) {
        if (!cancelled) router.push("/");
        return;
      }
      await load();
    })();
    return () => {
      cancelled = true;
    };
  }, [load, router]);

  const down = health
    ? Object.entries(health.services)
        .filter(([, s]) => !(s as { ok: boolean }).ok)
        .map(([name]) => name)
    : [];

  return (
    <AppShell
      workspaces={workspaces ?? []}
      activeWorkspaceId={activeWorkspaceId}
      email={email}
      onSignOut={() => {
        setToken(null);
        router.push("/");
      }}
    >
      <div className="h-full overflow-y-auto scroll-thin">
        <div className="mx-auto max-w-[1160px] px-8 py-9">
          <h1 className="text-[24px] font-semibold tracking-[-0.02em] text-ink">{title}</h1>
          {subtitle && <p className="mt-1 text-[13.5px] text-ink-2">{subtitle}</p>}

          {down.length > 0 && (
            <div className="mt-4">
              <Banner tone="warn">
                {down.join(", ")} {down.length === 1 ? "is" : "are"} unreachable. Figures on
                this page may be incomplete until {down.length === 1 ? "it comes" : "they come"}{" "}
                back.
              </Banner>
            </div>
          )}
          {error && (
            <div className="mt-4">
              <Banner tone="error">{error}</Banner>
            </div>
          )}

          <div className="mt-6">
            {workspaces === null ? (
              <div className="grid place-items-center py-16 text-ink-3">
                <Spinner />
              </div>
            ) : (
              children(workspaces)
            )}
          </div>
        </div>
      </div>
    </AppShell>
  );
}
