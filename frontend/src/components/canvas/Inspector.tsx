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
import { NODE_BY_KEY, STATUS_STYLE, type NodeKey } from "@/lib/graph";
import { ReviewDecisions } from "./ReviewDecisions";
import {
  Banner,
  Button,
  DownloadIcon,
  Eyebrow,
  PlayIcon,
  Row,
  Spinner,
} from "@/components/ui/primitives";
import type { NodeState } from "@/lib/useWorkspaceGraph";

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
}: {
  workspaceId: string;
  node: NodeState;
  events: { id: string; at: string; message: string; kind: string; severity: string }[];
  streaming: boolean;
  currency: string;
  onClose: () => void;
  onRun: (key: NodeKey) => void;
  onDownload: (key: NodeKey) => void;
  onResolved: () => void;
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
          </div>
        )}

        {tab === "inputs" && <InputsTab node={node} />}
        {tab === "output" && node.key === "review" && (
          <ReviewDecisions workspaceId={workspaceId} onResolved={onResolved} />
        )}
        {tab === "output" && node.key !== "review" && (
          <OutputTab
            workspaceId={workspaceId}
            node={node}
            currency={currency}
            onDownload={onDownload}
          />
        )}
        {tab === "logs" && <LogsTab events={events} streaming={streaming} />}
      </div>
    </aside>
  );
}

function InputsTab({ node }: { node: NodeState }) {
  const def = NODE_BY_KEY[node.key];
  if (def.needs.length === 0) {
    return (
      <div>
        <p className="text-[12.5px] leading-relaxed text-ink-2">
          This node reads from outside the workspace — the sources you connect. Nothing
          on the canvas feeds it.
        </p>
        <div className="mt-3">
          <Row label="Records produced" mono>
            {node.outCount ?? "—"}
          </Row>
        </div>
      </div>
    );
  }
  return (
    <div>
      <Eyebrow>Reads from</Eyebrow>
      <div className="mt-2 space-y-1.5">
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
      <div className="mt-3.5">
        <Row label="Records read" mono>
          {node.inCount ?? "—"}
        </Row>
      </div>
    </div>
  );
}

/** The output tab pulls the node's real figures from the endpoint that owns them. */
function OutputTab({
  workspaceId,
  node,
  currency,
  onDownload,
}: {
  workspaceId: string;
  node: NodeState;
  currency: string;
  onDownload: (key: NodeKey) => void;
}) {
  const [rows, setRows] = useState<[string, string][] | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      setRows(null);
      try {
        const out: [string, string][] = [];
        if (node.key === "identity") {
          const r = await api.listResolvedCustomers(workspaceId);
          out.push(["Customers resolved", String(r.customers.length)]);
          out.push([
            "Under review",
            String(r.customers.filter((c) => !c.human_confirmed && c.match_confidence !== null).length),
          ]);
        } else if (node.key === "contracts") {
          const r = await api.listContracts(workspaceId);
          const read = r.contracts.filter((c) => c.recurring_amount.minor > 0 || c.one_time_amount.minor > 0);
          out.push(["Contracts", String(r.contracts.length)]);
          out.push(["Read successfully", String(read.length)]);
        } else if (node.key === "reconcile") {
          const r = await api.reconciliation(workspaceId);
          out.push(["Solver", r.solver_status ?? "—"]);
          out.push(["Retained cash", r.totals?.retained?.display ?? "—"]);
          out.push(["Allocations written", String(r.allocations_written)]);
          out.push(["Invoices unpaid", String(r.invoices_unpaid)]);
        } else if (node.key === "revenue") {
          const r = await api.revenueSummary(workspaceId);
          out.push(["Claimed", fromMinor(r.totals.claimed_revenue, r.totals.currency)]);
          out.push([
            "Evidence-supported",
            fromMinor(r.totals.total_verified, r.totals.currency),
          ]);
          out.push(["Items classified", String(r.items_classified)]);
        } else if (node.key === "anomalies") {
          const r = await api.listAnomalies(workspaceId);
          out.push(["Indicators", String(r.anomalies.length)]);
          out.push([
            "High severity",
            String(r.anomalies.filter((a) => a.severity === "high").length),
          ]);
        } else if (node.key === "critic" || node.key === "review") {
          const r = await api.listReview(workspaceId);
          out.push(["Open decisions", String(r.summary.open_decisions)]);
          out.push(["Resolved", String(r.summary.resolved)]);
        } else if (node.key === "publish") {
          const r = await api.diligenceRoom(workspaceId);
          out.push(["Claimed", fromMinor(r.position.claimed_revenue, currency)]);
          out.push(["Proven and published", fromMinor(r.position.cash_received, currency)]);
          out.push(["Items published", String(r.position.items_published)]);
        } else if (node.key === "evidence") {
          const r = await api.workspaceSummary(workspaceId);
          for (const [k, v] of Object.entries(r.evidence_counts)) {
            out.push([k.replace(/_/g, " "), String(v)]);
          }
        }
        if (!cancelled) setRows(out);
      } catch {
        if (!cancelled) setRows([]);
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    if (node.outCount !== null) void load();
    return () => {
      cancelled = true;
    };
  }, [node.key, node.outCount, workspaceId, currency]);

  if (node.outCount === null) {
    return (
      <div className="rounded-[8px] border border-dashed border-line px-4 py-8 text-center">
        <p className="text-[12.5px] font-medium text-ink">No output yet</p>
        <p className="mt-1 text-[11.5px] text-ink-2">
          Run this node to produce something to inspect.
        </p>
      </div>
    );
  }

  return (
    <div>
      {loading && <Spinner className="text-ink-3" />}
      {rows && rows.length > 0 && (
        <div className="mb-3.5">
          {rows.map(([label, value]) => (
            <Row key={label} label={label} mono>
              {value}
            </Row>
          ))}
        </div>
      )}
      <Button className="w-full" onClick={() => onDownload(node.key)} icon={<DownloadIcon />}>
        Download this node&rsquo;s output
      </Button>
    </div>
  );
}

function LogsTab({
  events,
  streaming,
}: {
  events: { id: string; at: string; message: string; kind: string; severity: string }[];
  streaming: boolean;
}) {
  return (
    <div>
      <div className="mb-2 flex items-center justify-between">
        <Eyebrow>Live events</Eyebrow>
        <span className="flex items-center gap-1.5 text-[11px] text-ink-3">
          <span
            className={`h-[6px] w-[6px] rounded-full ${
              streaming ? "running-dot bg-emerald" : "bg-slate"
            }`}
          />
          {streaming ? "Streaming" : "Idle"}
        </span>
      </div>
      {events.length === 0 ? (
        <p className="text-[12px] text-ink-3">Nothing yet. Run a node to see what it does.</p>
      ) : (
        <ol className="space-y-1.5">
          {[...events].reverse().map((event) => (
            <li key={event.id} className="flex gap-2">
              <span className="shrink-0 font-mono text-[10.5px] tabular-nums text-ink-3">
                {event.at ? new Date(event.at).toLocaleTimeString(undefined, { hour12: false }) : "--:--:--"}
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
