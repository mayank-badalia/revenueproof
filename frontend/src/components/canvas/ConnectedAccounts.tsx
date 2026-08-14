"use client";

/**
 * The four provider accounts this deployment can already reach, and how to point one
 * at your own instead.
 *
 * The distinction this has to make, and make plainly: the accounts below belong to the
 * demonstration, not to whoever is looking at the screen. Someone evaluating the
 * product should be able to run a whole verification without connecting anything, and
 * should also be able to tell instantly that the records they are looking at are not
 * their company's.
 */

import { useState } from "react";

import { api, ApiError } from "@/lib/api";
import { Banner, Button, Input, Spinner } from "@/components/ui/primitives";
import type { ConnectionStatus } from "@/lib/types";

const PROVIDERS: { id: string; name: string; holds: string }[] = [
  { id: "razorpay", name: "Razorpay", holds: "Payments, refunds, disputes, settlements" },
  { id: "zoho_books", name: "Zoho Books", holds: "Invoices, credit notes, customers" },
  { id: "hubspot", name: "HubSpot", holds: "Companies and contacts" },
  { id: "google_drive", name: "Google Drive", holds: "Contract PDFs, read-only" },
];

export function ConnectedAccounts({
  workspaceId,
  connections,
  deploymentProviders,
  onChanged,
}: {
  workspaceId: string;
  connections: ConnectionStatus[];
  deploymentProviders?: Record<string, boolean>;
  onChanged: () => void;
}) {
  const [replacing, setReplacing] = useState<string | null>(null);
  const [token, setToken] = useState("");
  const [account, setAccount] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const byId = new Map(connections.map((c) => [String(c.source_system), c]));

  async function connect(providerId: string) {
    if (!token.trim()) {
      setError("Paste the read-only token for this provider.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.connectSource(workspaceId, providerId, token.trim(), account.trim() || undefined);
      setReplacing(null);
      setToken("");
      setAccount("");
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not connect that account");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <Banner tone="info">
        <strong className="font-semibold">These are the demo account&rsquo;s connections.</strong>{" "}
        All four are already signed in on this deployment, so you can run a full
        verification without connecting anything. They are real and read-only, and they
        belong to the demo — not to you. Use <em>Replace</em> to point any of them at your
        own account; it affects this workspace only.
      </Banner>

      <div className="mt-2 space-y-1.5">
        {PROVIDERS.map((provider) => {
          const connection = byId.get(provider.id);
          const reachable = deploymentProviders?.[provider.id] ?? false;
          const live = connection ? connection.is_active && !connection.is_synthetic : reachable;
          const open = replacing === provider.id;

          return (
            <div key={provider.id} className="rounded-[7px] border border-line">
              <div className="flex items-start gap-2.5 px-2.5 py-2">
                <span
                  className={`mt-[5px] h-[6px] w-[6px] shrink-0 rounded-full ${
                    live ? "bg-emerald" : connection ? "bg-amber" : "bg-slate"
                  }`}
                />
                <div className="min-w-0 flex-1">
                  <div className="flex items-baseline gap-2">
                    <span className="text-[12.5px] font-medium text-ink">{provider.name}</span>
                    <span
                      className={`text-[10.5px] ${
                        live ? "text-emerald" : connection ? "text-amber" : "text-ink-3"
                      }`}
                    >
                      {live ? "connected" : connection ? "demonstration data" : "not connected"}
                    </span>
                  </div>
                  <div className="mt-0.5 text-[11px] text-ink-3">{provider.holds}</div>
                  {connection?.last_sync_error && (
                    <div className="mt-0.5 text-[10.5px] text-rust">
                      {connection.last_sync_error}
                    </div>
                  )}
                  {connection?.records_imported ? (
                    <div className="mt-0.5 font-mono text-[10.5px] text-ink-3">
                      {connection.records_imported} records imported
                      {connection.last_sync_at
                        ? ` · ${new Date(connection.last_sync_at).toLocaleString()}`
                        : ""}
                    </div>
                  ) : null}
                </div>
                <Button
                  size="sm"
                  onClick={() => {
                    setReplacing(open ? null : provider.id);
                    setError(null);
                    setToken("");
                  }}
                >
                  {open ? "Cancel" : "Replace"}
                </Button>
              </div>

              {open && (
                <div className="space-y-2 border-t border-line-2 px-2.5 py-2.5">
                  <p className="text-[11px] leading-relaxed text-ink-2">
                    Paste a read-only token. It is encrypted before storage and is never
                    included in any prompt sent to the language model.
                  </p>
                  <Input
                    type="password"
                    value={token}
                    onChange={(e) => setToken(e.target.value)}
                    placeholder="Read-only access token"
                    className="h-8 text-[12px]"
                    autoComplete="off"
                  />
                  <Input
                    value={account}
                    onChange={(e) => setAccount(e.target.value)}
                    placeholder="Account or organisation id (optional)"
                    className="h-8 text-[12px]"
                  />
                  {error && <Banner tone="error">{error}</Banner>}
                  <Button
                    variant="primary"
                    size="sm"
                    className="w-full"
                    disabled={busy || !token.trim()}
                    onClick={() => void connect(provider.id)}
                    icon={busy ? <Spinner /> : undefined}
                  >
                    {busy ? "Connecting…" : `Use my ${provider.name}`}
                  </Button>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
