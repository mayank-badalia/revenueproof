"use client";

/**
 * The landing route: sign in, then the workspace dashboard.
 */

import { useCallback, useEffect, useState } from "react";

import { api, getToken, setToken } from "@/lib/api";
import { AppShell } from "@/components/shell/AppShell";
import { AuthGate } from "@/components/AuthGate";
import { Dashboard } from "@/components/dashboard/Dashboard";
import { Spinner } from "@/components/ui/primitives";
import type { Workspace } from "@/lib/types";

export default function HomePage() {
  const [authed, setAuthed] = useState<boolean | null>(null);
  const [email, setEmail] = useState<string | null>(null);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);

  const load = useCallback(async () => {
    try {
      const [me, list] = await Promise.all([api.me(), api.listWorkspaces()]);
      setEmail(me.email);
      setWorkspaces(list);
      setAuthed(true);
    } catch {
      setAuthed(false);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      if (!getToken()) {
        if (!cancelled) setAuthed(false);
        return;
      }
      await load();
    })();
    return () => {
      cancelled = true;
    };
  }, [load]);

  if (authed === null) {
    return (
      <div className="grid h-screen place-items-center text-ink-3">
        <Spinner />
      </div>
    );
  }

  if (!authed) return <AuthGate onAuthenticated={() => void load()} />;

  return (
    <AppShell
      workspaces={workspaces}
      email={email}
      onSignOut={() => {
        setToken(null);
        setAuthed(false);
      }}
    >
      <Dashboard workspaces={workspaces} email={email} onChanged={() => void load()} />
    </AppShell>
  );
}
