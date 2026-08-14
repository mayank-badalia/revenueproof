"use client";

/**
 * One node on the canvas.
 *
 * The card is built to be read in a glance from across a desk: a coloured spine down
 * the left edge carries the status, so a reviewer scanning ten nodes reads a column
 * of spines rather than ten pills. The in/out pair at the foot is set in tabular
 * figures and reads like a ledger line — records in, records out — because that is
 * exactly what it is, and because a node that consumed 55 invoices and produced 7
 * allocations is telling you something a status word cannot.
 */

import { memo } from "react";
import { Handle, Position, type NodeProps } from "reactflow";

import { NODE_BY_KEY, STATUS_STYLE, type NodeKey, type NodeStatus } from "@/lib/graph";
import { duration } from "@/lib/format";
import { CheckIcon, DownloadIcon, LockIcon, PlayIcon, Spinner } from "@/components/ui/primitives";

export interface VerificationNodeData {
  nodeKey: NodeKey;
  status: NodeStatus;
  inCount: number | null;
  outCount: number | null;
  seconds: number | null;
  error: string | null;
  lockedReason: string | null;
  selected: boolean;
  onRun: (key: NodeKey) => void;
  onDownload: (key: NodeKey) => void;
  onOpen: (key: NodeKey) => void;
}

function VerificationNodeInner({ data }: NodeProps<VerificationNodeData>) {
  const def = NODE_BY_KEY[data.nodeKey];
  const style = STATUS_STYLE[data.status];
  const isLocked = data.status === "locked";
  const isRunning = data.status === "running";
  const canRun = !isLocked && !isRunning;
  const hasOutput =
    data.status === "complete" || data.status === "stale" || data.status === "waiting";

  return (
    <div
      onClick={() => data.onOpen(data.nodeKey)}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          data.onOpen(data.nodeKey);
        }
      }}
      aria-label={`${def.title} — ${style.label}`}
      className={`group relative w-[236px] cursor-pointer overflow-hidden rounded-node border bg-paper transition-shadow ${
        data.selected
          ? "border-cobalt shadow-[0_0_0_3px_rgba(37,99,235,0.14)]"
          : `${style.border} shadow-[0_1px_2px_rgba(15,23,42,0.05)] hover:shadow-[0_3px_10px_rgba(15,23,42,0.09)]`
      } ${isLocked ? "opacity-[0.72]" : ""}`}
    >
      <Handle type="target" position={Position.Left} className="!-left-[4px]" />
      <span className={`absolute inset-y-0 left-0 w-[3px] ${style.rail}`} aria-hidden />

      <div className="pl-[13px] pr-2.5 pt-2.5">
        <div className="flex items-start gap-2">
          <span className="mt-[1px] font-mono text-[10.5px] font-semibold tabular-nums text-ink-3">
            {def.ord}
          </span>
          <span className="min-w-0 flex-1">
            <span className="block truncate text-[13px] font-semibold leading-tight tracking-[-0.01em] text-ink">
              {def.title}
            </span>
          </span>
          {isLocked && <LockIcon />}
        </div>

        <p className="mt-1 line-clamp-2 text-[11.5px] leading-[1.45] text-ink-2">
          {isLocked && data.lockedReason ? data.lockedReason : def.summary}
        </p>

        <div className="mt-2 flex items-center gap-1.5">
          <span
            className={`inline-flex items-center gap-1.5 rounded-full px-2 py-[3px] text-[10.5px] font-medium ${style.bg} ${style.text}`}
          >
            {isRunning ? (
              <Spinner className="h-[10px] w-[10px]" />
            ) : data.status === "complete" ? (
              <CheckIcon size={10} />
            ) : (
              <span className={`h-[5px] w-[5px] rounded-full ${style.dot}`} />
            )}
            {style.label}
          </span>
          {data.seconds !== null && !isRunning && (
            <span className="font-mono text-[10.5px] tabular-nums text-ink-3">
              {duration(data.seconds)}
            </span>
          )}
        </div>
      </div>

      {data.error && (
        <p className="mx-[13px] mt-2 truncate rounded-[5px] bg-rust-soft px-2 py-1 text-[10.5px] text-rust">
          {data.error}
        </p>
      )}

      <div className="mt-2.5 flex items-center justify-between border-t border-line-2 bg-slate-soft/45 py-1.5 pl-[13px] pr-1.5">
        <span className="flex items-center gap-2.5 font-mono text-[10.5px] tabular-nums text-ink-2">
          <span>
            <span className="text-ink-3">In</span>{" "}
            {data.inCount === null ? "—" : data.inCount}
          </span>
          <span>
            <span className="text-ink-3">Out</span>{" "}
            {data.outCount === null ? "—" : data.outCount}
          </span>
        </span>

        <span className="flex items-center gap-0.5">
          <button
            onClick={(e) => {
              e.stopPropagation();
              data.onRun(data.nodeKey);
            }}
            disabled={!canRun}
            aria-label={`Run ${def.title}`}
            title={isLocked ? (data.lockedReason ?? "Locked") : `Run ${def.title}`}
            className="grid h-6 w-6 place-items-center rounded-[5px] text-ink-2 transition-colors hover:bg-cobalt-soft hover:text-cobalt disabled:cursor-not-allowed disabled:text-ink-3 disabled:hover:bg-transparent"
          >
            {isRunning ? <Spinner /> : <PlayIcon />}
          </button>
          <button
            onClick={(e) => {
              e.stopPropagation();
              data.onDownload(data.nodeKey);
            }}
            disabled={!hasOutput}
            aria-label={`Download ${def.title} output`}
            title={hasOutput ? `Download ${def.title} output` : "Nothing to download yet"}
            className="grid h-6 w-6 place-items-center rounded-[5px] text-ink-2 transition-colors hover:bg-cobalt-soft hover:text-cobalt disabled:cursor-not-allowed disabled:text-ink-3 disabled:hover:bg-transparent"
          >
            <DownloadIcon />
          </button>
        </span>
      </div>

      <Handle type="source" position={Position.Right} className="!-right-[4px]" />
    </div>
  );
}

export const VerificationNode = memo(VerificationNodeInner);
