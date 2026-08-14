"use client";

/**
 * The right-hand inspector: everything known about one node.
 *
 * Four tabs, because a reviewer asks four different questions and mixing them into
 * one scroll makes all four harder. Overview is what this node is and where it stands;
 * Inputs is what it read; Output is what it produced and how to take it away; Logs is
 * what it actually did, live.
 */

import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import { duration, fromMinor } from "@/lib/format";
import { NODE_BY_KEY, NODE_FEATURE, STATUS_STYLE, type NodeKey } from "@/lib/graph";
import { NodeOutput } from "./NodeOutput";
import {
  Banner,
  Button,
  DownloadIcon,
  Eyebrow,
  PlayIcon,
  Row,
  Spinner,
} from "@/components/ui/primitives";
import type { NodeState, TraceLine } from "@/lib/useWorkspaceGraph";

type Tab = "overview" | "inputs" | "output" | "logs";

export function Inspector({
  workspaceId,
  node,
  events,
  streaming,
  currency,
  onClose,
  onRun,
  onDownload,
  onResolved,
  onRemove,
  counts,
}: {
  workspaceId: string;
  node: NodeState;
  events: TraceLine[];
  streaming: boolean;
  currency: string;
  onClose: () => void;
  onRun: (key: NodeKey) => void;
  onDownload: (key: NodeKey) => void;
  onResolved: () => void;
  onRemove: (key: NodeKey) => void;
  counts: Record<string, number>;
}) {
  const [tab, setTab] = useState<Tab>("overview");
  const def = NODE_BY_KEY[node.key];
  const style = STATUS_STYLE[node.status];

  return (
    <aside className="flex h-full w-[352px] shrink-0 flex-col border-l border-line bg-paper">
      <header className="flex items-start justify-between gap-3 border-b border-line px-4 py-3">
        <div className="min-w-0">
          <div className="flex items-baseline gap-2">
            <span className="font-mono text-[11px] font-semibold tabular-nums text-ink-3">
              {def.ord}
            </span>
            <h2 className="truncate text-[14.5px] font-semibold tracking-[-0.01em] text-ink">
              {def.title}
            </h2>
          </div>
          <p className="mt-0.5 text-[11.5px] text-ink-2">{def.agent}</p>
        </div>
        <button
          onClick={onClose}
          aria-label="Close inspector"
          className="rounded p-1 text-ink-3 transition-colors hover:bg-slate-soft hover:text-ink"
        >
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden>
            <path d="m4 4 8 8M12 4l-8 8" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
          </svg>
        </button>
      </header>

      <nav className="flex gap-0.5 border-b border-line px-2" role="tablist">
        {(["overview", "inputs", "output", "logs"] as Tab[]).map((id) => (
          <button
            key={id}
            role="tab"
            aria-selected={tab === id}
            onClick={() => setTab(id)}
            className={`relative px-2.5 py-2 text-[12.5px] capitalize transition-colors ${
              tab === id ? "font-medium text-cobalt" : "text-ink-2 hover:text-ink"
            }`}
          >
            {id}
            {tab === id && <span className="absolute inset-x-2 -bottom-px h-[2px] rounded-full bg-cobalt" />}
          </button>
        ))}
      </nav>

      <div className="min-h-0 flex-1 overflow-y-auto scroll-thin px-4 py-3.5">
        {tab === "overview" && (
          <div className="space-y-4">
            <div>
              <Row label="Status">
                <span className={`inline-flex items-center gap-1.5 ${style.text}`}>
                  <span className={`h-[6px] w-[6px] rounded-full ${style.dot}`} />
                  {style.label}
                </span>
              </Row>
              <Row label="Agent">{def.agent}</Row>
              <Row label="Records in" mono>
                {node.inCount ?? "—"}
              </Row>
              <Row label="Records out" mono>
                {node.outCount ?? "—"}
              </Row>
              <Row label="Elapsed" mono>
                {duration(node.seconds)}
              </Row>
              <Row label="Depends on">
                {def.needs.length === 0
                  ? "Nothing — this is the start"
                  : def.needs.map((k) => NODE_BY_KEY[k].title).join(", ")}
              </Row>
            </div>

            {node.error && <Banner tone="error">{node.error}</Banner>}
            {node.status === "stale" && (
              <Banner tone="warn">
                An upstream node has re-run since this one did, so its output no longer
                reflects the current evidence. Run it again.
              </Banner>
            )}
            {node.status === "locked" && (
              <Banner tone="warn">
                Locked until {def.needs.map((k) => NODE_BY_KEY[k].title).join(" and ")} has
                run.
              </Banner>
            )}

            <div>
              <Eyebrow>Why this node exists</Eyebrow>
              <p className="mt-1.5 text-[12.5px] leading-relaxed text-ink-2">
                {def.rationale}
              </p>
            </div>

            <div className="flex gap-2">
              <Button
                variant="primary"
                className="flex-1"
                disabled={node.status === "locked" || node.status === "running"}
                onClick={() => onRun(node.key)}
                icon={node.status === "running" ? <Spinner /> : <PlayIcon />}
              >
                {node.status === "running" ? "Running…" : "Run this node"}
              </Button>
              <Button
                disabled={node.outCount === null}
                onClick={() => onDownload(node.key)}
                icon={<DownloadIcon />}
                aria-label="Download output"
              />
            </div>

            {node.key !== "evidence" && (
              <button
                onClick={() => onRemove(node.key)}
                className="w-full rounded-[7px] py-1.5 text-[12px] text-ink-3 transition-colors hover:bg-rust-soft hover:text-rust"
              >
                Remove from canvas
              </button>
            )}
          </div>
        )}

        {tab === "inputs" && (
          <InputsTab node={node} counts={counts} currency={currency} />
        )}
        {tab === "output" && (
          <NodeOutput
            workspaceId={workspaceId}
            nodeKey={node.key}
            currency={currency}
            hasRun={node.outCount !== null || node.status === "waiting"}
            onResolved={onResolved}
          />
        )}
        {tab === "logs" && (
          <LogsTab events={events} streaming={streaming} nodeKey={node.key} />
        )}
      </div>
    </aside>
  );
}

function InputsTab({
  node,
  counts,
  currency,
}: {
  node: NodeState;
  counts: Record<string, number>;
  currency: string;
}) {
  const def = NODE_BY_KEY[node.key];
  const reads = INPUT_SOURCES[node.key] ?? [];

  return (
    <div>
      <Eyebrow>What this node reads</Eyebrow>
      <div className="mt-1.5 space-y-1">
        {reads.length === 0 ? (
          <p className="text-[12.5px] leading-relaxed text-ink-2">
            Nothing on the canvas feeds this node — it reads from outside the workspace,
            from the sources you connect.
          </p>
        ) : (
          reads.map(([label, key]) => (
            <Row key={label} label={label} mono>
              {counts[key] ?? 0}
            </Row>
          ))
        )}
      </div>

      {def.needs.length > 0 && (
        <div className="mt-4">
          <Eyebrow>Fed by</Eyebrow>
          <div className="mt-1.5 space-y-1.5">
            {def.needs.map((key) => {
              const upstream = NODE_BY_KEY[key];
              return (
                <div
                  key={key}
                  className="flex items-center gap-2.5 rounded-[7px] border border-line px-3 py-2"
                >
                  <span className="font-mono text-[10.5px] font-semibold tabular-nums text-ink-3">
                    {upstream.ord}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-[12.5px] font-medium text-ink">
                      {upstream.title}
                    </span>
                    <span className="block text-[11.5px] text-ink-3">{upstream.emits}</span>
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      )}

      <div className="mt-4">
        <Row label="Records read" mono>
          {node.inCount ?? "—"}
        </Row>
        <Row label="Base currency" mono>
          {currency}
        </Row>
      </div>
    </div>
  );
}

/** The record types each node consumes, named as a reviewer would name them. */
const INPUT_SOURCES: Partial<Record<NodeKey, [string, string][]>> = {
  identity: [
    ["Raw records", "raw_records"],
    ["Customer records", "customers"],
  ],
  contracts: [["Contract documents", "contracts"]],
  reconcile: [
    ["Invoices", "invoices"],
    ["Payments", "payments"],
    ["Bank transactions", "bank_transactions"],
    ["Refunds", "refunds"],
  ],
  revenue: [
    ["Allocations", "allocations"],
    ["Invoices", "invoices"],
    ["Contracts", "contracts"],
  ],
  anomalies: [
    ["Revenue items", "revenue_items"],
    ["Customers", "customers"],
  ],
  critic: [["Revenue items", "revenue_items"]],
  review: [["Critic decisions", "critic_decisions"]],
  publish: [
    ["Revenue items", "revenue_items"],
    ["Critic decisions", "critic_decisions"],
  ],
};

function LogsTab({
  events,
  streaming,
  nodeKey,
}: {
  events: TraceLine[];
  streaming: boolean;
  nodeKey: NodeKey;
}) {
  /* Only this node's lines. The trace carries the feature that emitted each event, so
     "logs" on a node can mean that node rather than everything the workspace has ever
     done — which is what made the panel unreadable and identical on all nine. */
  const feature = NODE_FEATURE[nodeKey];
  const mine = events.filter((e) => e.feature === feature);
  const shown = mine.length > 0 ? mine : events;

  return (
    <div>
      <div className="mb-2 flex items-center justify-between">
        <Eyebrow>
          {mine.length > 0 ? "This node" : "Workspace"} · {shown.length} events
        </Eyebrow>
        <span className="flex items-center gap-1.5 text-[11px] text-ink-3">
          <span
            className={`h-[6px] w-[6px] rounded-full ${
              streaming ? "running-dot bg-emerald" : "bg-slate"
            }`}
          />
          {streaming ? "Streaming" : "Idle"}
        </span>
      </div>
      {shown.length === 0 ? (
        <p className="text-[12px] text-ink-3">Nothing yet. Run this node to see what it does.</p>
      ) : (
        <ol className="space-y-1.5">
          {[...shown].reverse().map((event) => (
            <li key={event.id} className="flex gap-2">
              <span
                className="shrink-0 font-mono text-[10.5px] tabular-nums text-ink-3"
                title={event.at ? new Date(event.at).toLocaleString() : undefined}
              >
                {clockOf(event.at)}
              </span>
              <span
                className={`text-[11.5px] leading-snug ${
                  event.severity === "error"
                    ? "text-rust"
                    : event.severity === "warning"
                      ? "text-amber"
                      : "text-ink-2"
                }`}
              >
                {event.message}
              </span>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

/**
 * The backend stamps events in UTC with an offset, so the browser converts correctly —
 * but a bare wall clock on a line from yesterday reads as though it happened moments
 * ago. Anything not from today carries its date.
 */
function clockOf(iso: string): string {
  if (!iso) return "--:--:--";
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return "--:--:--";
  const time = at.toLocaleTimeString(undefined, { hour12: false });
  const now = new Date();
  const sameDay =
    at.getDate() === now.getDate() &&
    at.getMonth() === now.getMonth() &&
    at.getFullYear() === now.getFullYear();
  return sameDay
    ? time
    : `${at.toLocaleDateString(undefined, { day: "2-digit", month: "short" })} ${time}`;
}
