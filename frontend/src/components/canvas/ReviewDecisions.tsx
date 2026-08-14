"use client";

/**
 * The open decisions, resolved from inside the inspector.
 *
 * A decision cannot be recorded without a written reason — the backend refuses one,
 * and so does this form. §7 requires an override to say why, because a figure that
 * moved for unstated reasons is not auditable, and a disabled button that explains
 * itself is better than a rejected request that does not.
 */

import { useCallback, useEffect, useState } from "react";

import { api, ApiError } from "@/lib/api";
import { Banner, Button, Eyebrow, Spinner } from "@/components/ui/primitives";
import type { ReviewItemRow } from "@/lib/types";

const DECISIONS: { id: string; label: string; tone: string }[] = [
  { id: "approved", label: "Confirm", tone: "text-emerald" },
  { id: "rejected", label: "Not an issue", tone: "text-ink-2" },
  { id: "escalated", label: "Escalate", tone: "text-amber" },
];

export function ReviewDecisions({
  workspaceId,
  onResolved,
}: {
  workspaceId: string;
  onResolved: () => void;
}) {
  const [items, setItems] = useState<ReviewItemRow[] | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);
  const [decision, setDecision] = useState("approved");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const queue = await api.listReview(workspaceId);
      setItems(queue.items);
    } catch {
      setItems([]);
    }
  }, [workspaceId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function resolve(itemId: string) {
    if (!reason.trim()) {
      setError("A decision needs a reason. It goes into the audit log beside the change.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.resolveReview(workspaceId, itemId, decision, reason.trim());
      setOpenId(null);
      setReason("");
      await load();
      onResolved();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not record that decision");
    } finally {
      setBusy(false);
    }
  }

  if (items === null) return <Spinner className="text-ink-3" />;

  if (items.length === 0) {
    return (
      <div className="rounded-[8px] border border-dashed border-line px-4 py-8 text-center">
        <p className="text-[12.5px] font-medium text-emerald">Nothing waiting</p>
        <p className="mt-1 text-[11.5px] text-ink-2">
          Every question the agents raised has been settled.
        </p>
      </div>
    );
  }

  return (
    <div>
      <Eyebrow>
        {items.length} open {items.length === 1 ? "decision" : "decisions"}
      </Eyebrow>
      <div className="mt-2 space-y-2">
        {items.map((item) => {
          const expanded = openId === item.id;
          return (
            <div key={item.id} className="rounded-[8px] border border-line">
              <button
                onClick={() => {
                  setOpenId(expanded ? null : item.id);
                  setReason("");
                  setError(null);
                }}
                className="flex w-full items-start gap-2 px-3 py-2.5 text-left"
              >
                <span
                  className={`mt-[5px] h-[6px] w-[6px] shrink-0 rounded-full ${
                    item.severity === "high"
                      ? "bg-rust"
                      : item.severity === "medium"
                        ? "bg-amber"
                        : "bg-slate"
                  }`}
                />
                <span className="min-w-0 flex-1">
                  <span className="block text-[12.5px] font-medium leading-snug text-ink">
                    {item.title}
                  </span>
                  <span className="mt-0.5 block text-[11.5px] text-ink-3">
                    {item.raised_by}
                    {item.member_count > 1 && ` · covers ${item.member_count} records`}
                  </span>
                </span>
              </button>

              {expanded && (
                <div className="space-y-2.5 border-t border-line-2 px-3 py-2.5">
                  {item.detail && (
                    <p className="text-[11.5px] leading-relaxed text-ink-2">{item.detail}</p>
                  )}

                  <div className="flex gap-1">
                    {DECISIONS.map((option) => (
                      <button
                        key={option.id}
                        onClick={() => setDecision(option.id)}
                        className={`h-7 flex-1 rounded-[6px] text-[11.5px] font-medium transition-colors ${
                          decision === option.id
                            ? "bg-cobalt text-white"
                            : "bg-paper ring-1 ring-line hover:bg-slate-soft"
                        }`}
                      >
                        {option.label}
                      </button>
                    ))}
                  </div>

                  <textarea
                    value={reason}
                    onChange={(e) => setReason(e.target.value)}
                    rows={2}
                    placeholder="Why? This is recorded in the audit log."
                    className="w-full resize-none rounded-[6px] border border-line px-2.5 py-1.5 text-[12px] placeholder:text-ink-3 focus:border-cobalt focus:outline-none"
                  />

                  {error && <Banner tone="error">{error}</Banner>}

                  <Button
                    variant="primary"
                    size="sm"
                    className="w-full"
                    disabled={busy || !reason.trim()}
                    onClick={() => void resolve(item.id)}
                    icon={busy ? <Spinner /> : undefined}
                  >
                    {busy ? "Recording…" : "Record decision"}
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
