"use client";

/**
 * The execution strip along the bottom.
 *
 * Nine dots on a rule, one per node, in the order the chain runs. It answers the one
 * question the canvas cannot answer while you are panned into a corner of it: how far
 * along is this, and what is it doing right now. Collapsed it is a single line; the
 * canvas is the working surface and this is a status bar, not a second workspace.
 */

import { NODES, STATUS_STYLE, type NodeKey } from "@/lib/graph";
import { duration } from "@/lib/format";
import { Spinner } from "@/components/ui/primitives";
import type { NodeState } from "@/lib/useWorkspaceGraph";

export function ExecutionDrawer({
  nodes,
  open,
  onToggle,
  onSelect,
  currentLog,
  runId,
}: {
  nodes: Record<NodeKey, NodeState>;
  open: boolean;
  onToggle: () => void;
  onSelect: (key: NodeKey) => void;
  currentLog: string | null;
  runId: string | null;
}) {
  const onCanvas = NODES.filter((def) => nodes[def.key].onCanvas);
  const done = onCanvas.filter((def) =>
    ["complete", "waiting"].includes(nodes[def.key].status),
  ).length;
  const running = onCanvas.find((def) => nodes[def.key].status === "running");

  return (
    <div className="border-t border-line bg-paper">
      <button
        onClick={onToggle}
        aria-expanded={open}
        className="flex w-full items-center gap-3 px-4 py-2 text-left transition-colors hover:bg-slate-soft/60"
      >
        <span className="flex items-center gap-2">
          <svg
            width="12"
            height="12"
            viewBox="0 0 16 16"
            fill="none"
            aria-hidden
            className={`text-ink-3 transition-transform ${open ? "" : "rotate-180"}`}
          >
            <path d="m3.5 10 4.5-4.5 4.5 4.5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          <span className="text-[12.5px] font-medium text-ink">Execution</span>
          {runId && (
            <span className="font-mono text-[11px] uppercase tracking-wide text-ink-3">
              {runId.slice(0, 6)}
            </span>
          )}
        </span>

        <span className="font-mono text-[11.5px] tabular-nums text-ink-2">
          {done} of {onCanvas.length} nodes
        </span>

        {running ? (
          <span className="flex min-w-0 items-center gap-1.5 text-[12px] text-cobalt">
            <Spinner />
            <span className="truncate">{running.title}</span>
          </span>
        ) : (
          <span className="truncate text-[12px] text-ink-3">
            {currentLog ?? "Idle"}
          </span>
        )}
      </button>

      {open && (
        <div className="overflow-x-auto scroll-thin px-4 pb-3.5 pt-1">
          <div className="flex min-w-max items-start gap-1">
            {onCanvas.map((def, index) => {
              const state = nodes[def.key];
              const style = STATUS_STYLE[state.status];
              const isRunning = state.status === "running";
              return (
                <div key={def.key} className="flex items-start">
                  <button
                    onClick={() => onSelect(def.key)}
                    className="w-[104px] rounded-[6px] px-1.5 py-1.5 text-center transition-colors hover:bg-slate-soft"
                  >
                    <div className="font-mono text-[10px] tabular-nums text-ink-3">
                      {def.ord}
                    </div>
                    <div className="mt-0.5 truncate text-[11px] font-medium text-ink">
                      {def.title}
                    </div>
                    <div className="mt-1.5 flex justify-center">
                      {isRunning ? (
                        <span className="text-cobalt">
                          <Spinner />
                        </span>
                      ) : (
                        <span className={`h-[9px] w-[9px] rounded-full ${style.dot}`} />
                      )}
                    </div>
                    <div className={`mt-1 truncate text-[10.5px] ${style.text}`}>
                      {style.label}
                    </div>
                    <div className="font-mono text-[10px] tabular-nums text-ink-3">
                      {state.seconds !== null ? duration(state.seconds) : "—"}
                    </div>
                  </button>
                  {index < onCanvas.length - 1 && (
                    <span
                      className={`mt-[30px] h-[2px] w-3 rounded-full ${
                        ["complete", "waiting"].includes(state.status)
                          ? "bg-emerald/45"
                          : "bg-line"
                      }`}
                      aria-hidden
                    />
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
