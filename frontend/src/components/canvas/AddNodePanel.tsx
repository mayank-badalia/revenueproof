"use client";

/**
 * The node palette.
 *
 * Grouped by what the nodes are for rather than by run order, because the order is
 * already on the canvas and repeating it here would say nothing new. Every unavailable
 * entry states the thing to go and do — "Add Verify Revenue first" — since a disabled
 * row with no reason is just a dead end.
 */

import { useState } from "react";

import { GROUP_LABEL, NODES, type NodeDef, type NodeKey } from "@/lib/graph";
import { CheckIcon, LockIcon } from "@/components/ui/primitives";

function RestoreIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 16 16" fill="none" aria-hidden>
      <path
        d="M2.8 6.4h6.6a3.6 3.6 0 0 1 0 7.2H6M2.8 6.4l3-3M2.8 6.4l3 3"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function AddNodePanel({
  present,
  removed,
  onAdd,
  onClose,
}: {
  present: Set<NodeKey>;
  /** Cut off the canvas. Offered back first, because that is why the panel is open. */
  removed: Set<NodeKey>;
  onAdd: (key: NodeKey) => string | null;
  onClose: () => void;
}) {
  const [refusal, setRefusal] = useState<{ key: NodeKey; message: string } | null>(null);

  const groups = (["evidence", "verification", "controls", "output"] as const).map(
    (group) => ({ group, items: NODES.filter((n) => n.group === group) }),
  );

  function attempt(def: NodeDef) {
    const message = onAdd(def.key);
    if (message) setRefusal({ key: def.key, message });
    else {
      setRefusal(null);
      onClose();
    }
  }

  return (
    <div className="w-[268px] overflow-hidden rounded-[10px] border border-line bg-paper shadow-[0_14px_40px_-8px_rgba(8,17,31,0.24)]">
      <header className="flex items-center justify-between border-b border-line px-3.5 py-2.5">
        <h3 className="text-[13px] font-semibold text-ink">Add node</h3>
        <button
          onClick={onClose}
          aria-label="Close"
          className="rounded p-1 text-ink-3 hover:bg-slate-soft hover:text-ink"
        >
          <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden>
            <path d="m4 4 8 8M12 4l-8 8" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
          </svg>
        </button>
      </header>

      <div className="max-h-[420px] overflow-y-auto scroll-thin pb-1.5">
        {removed.size > 0 && (
          <div className="border-b border-line-2 pb-1.5">
            <div className="flex items-center justify-between px-3.5 pb-1 pt-2.5">
              <span className="text-[10px] font-semibold uppercase tracking-[0.09em] text-amber">
                Cut from this canvas
              </span>
              <button
                onClick={() => {
                  for (const key of removed) onAdd(key);
                  onClose();
                }}
                className="text-[10.5px] font-medium text-cobalt hover:underline"
              >
                Put all back
              </button>
            </div>
            {NODES.filter((def) => removed.has(def.key)).map((def) => (
              <button
                key={def.key}
                onClick={() => attempt(def)}
                className="flex w-full items-start gap-2.5 px-3.5 py-[7px] text-left transition-colors hover:bg-amber-soft"
              >
                <span className="mt-[2px] font-mono text-[10px] font-semibold tabular-nums text-ink-3">
                  {def.ord}
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[12.5px] font-medium text-ink">
                    {def.title}
                  </span>
                  <span className="mt-0.5 block text-[11px] text-amber">
                    Put it back — its output was kept
                  </span>
                </span>
                <span className="mt-[3px] text-cobalt">
                  <RestoreIcon />
                </span>
              </button>
            ))}
          </div>
        )}
        {groups.map(({ group, items }) => (
          <div key={group}>
            <div className="px-3.5 pb-1 pt-2.5 text-[10px] font-semibold uppercase tracking-[0.09em] text-ink-3">
              {GROUP_LABEL[group]}
            </div>
            {items.map((def) => {
              const already = present.has(def.key);
              const missing = def.needs.filter((need) => !present.has(need));
              const blocked = missing.length > 0;
              const showing = refusal?.key === def.key;
              return (
                <div key={def.key}>
                  <button
                    onClick={() => attempt(def)}
                    disabled={already}
                    className={`flex w-full items-start gap-2.5 px-3.5 py-[7px] text-left transition-colors ${
                      already
                        ? "cursor-not-allowed opacity-60"
                        : blocked
                          ? "hover:bg-amber-soft"
                          : "hover:bg-cobalt-soft"
                    }`}
                  >
                    <span className="mt-[2px] font-mono text-[10px] font-semibold tabular-nums text-ink-3">
                      {def.ord}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-[12.5px] font-medium text-ink">
                        {def.title}
                      </span>
                      <span
                        className={`mt-0.5 block text-[11px] leading-snug ${
                          already ? "text-emerald" : blocked ? "text-amber" : "text-ink-3"
                        }`}
                      >
                        {already
                          ? "Already added"
                          : blocked
                            ? `Requires ${missing.map((m) => NODES.find((n) => n.key === m)!.title).join(" and ")}`
                            : def.summary}
                      </span>
                    </span>
                    {already ? (
                      <span className="mt-[3px] text-emerald">
                        <CheckIcon size={11} />
                      </span>
                    ) : blocked ? (
                      <span className="mt-[3px] text-amber">
                        <LockIcon size={11} />
                      </span>
                    ) : null}
                  </button>
                  {showing && (
                    <p className="mx-3.5 mb-1.5 rounded-[5px] bg-amber-soft px-2 py-1 text-[11px] text-amber">
                      {refusal.message}
                    </p>
                  )}
                </div>
              );
            })}
          </div>
        ))}
      </div>
    </div>
  );
}
