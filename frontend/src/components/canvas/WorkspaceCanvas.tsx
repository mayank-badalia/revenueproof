"use client";

/**
 * The verification canvas.
 *
 * Run all does what its name says and nothing more clever: it asks the backend to run
 * the whole chain in dependency order, and the backend refuses if the workspace is
 * empty or a prerequisite has not run. The canvas then shows what actually happened.
 * Nothing here predicts a result, and no node turns green because the frontend
 * believes it should — every status comes back from counting real rows.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactFlow, {
  Background,
  BackgroundVariant,
  Controls,
  MarkerType,
  MiniMap,
  ReactFlowProvider,
  useNodesState,
  useReactFlow,
  type Edge,
} from "reactflow";

import { api, ApiError } from "@/lib/api";
import {
  DOWNLOAD_KEY,
  LAYOUT,
  NODES,
  NODE_BY_KEY,
  type NodeKey,
  type NodeStatus,
} from "@/lib/graph";
import { FEATURE_TO_NODE, useTrace, useWorkspaceGraph } from "@/lib/useWorkspaceGraph";
import { elapsedClock } from "@/lib/format";
import { Banner, Button, PlayIcon, Spinner } from "@/components/ui/primitives";
import { AddNodePanel } from "./AddNodePanel";
import { ExecutionDrawer } from "./ExecutionDrawer";
import { Inspector } from "./Inspector";
import { SourcePicker } from "./SourcePicker";
import { VerificationNode, type VerificationNodeData } from "./VerificationNode";
import type { WorkspaceSummary } from "@/lib/types";

const NODE_TYPES = { verification: VerificationNode };

/**
 * Re-frame the canvas when the graph grows.
 *
 * Run all can add seven nodes at once, and without this the viewport stays where it
 * was — which put the node the user had just been looking at off-screen and made the
 * new ones appear to have been added somewhere else entirely.
 */
function FitOnGrowth({ count }: { count: number }) {
  const flow = useReactFlow();
  const previous = useRef(count);
  useEffect(() => {
    if (count !== previous.current) {
      previous.current = count;
      // Long enough for React Flow to measure the new nodes. Fitting before they
      // have dimensions produces a degenerate bounding box and a useless zoom.
      const timer = window.setTimeout(
        () => flow.fitView({ padding: 0.2, maxZoom: 1, minZoom: 0.35, duration: 320 }),
        180,
      );
      return () => window.clearTimeout(timer);
    }
  }, [count, flow]);
  return null;
}

export function WorkspaceCanvas({
  workspaceId,
  summary,
}: {
  workspaceId: string;
  summary: WorkspaceSummary | null;
}) {
  const graph = useWorkspaceGraph(workspaceId);
  const { markRunning } = graph;
  const { events, streaming } = useTrace(workspaceId);

  const [selected, setSelected] = useState<NodeKey | null>(null);
  const [showAdd, setShowAdd] = useState(false);
  const [showSources, setShowSources] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(true);
  const [toast, setToast] = useState<{ tone: "warn" | "error" | "success"; text: string } | null>(
    null,
  );
  const [runningAll, setRunningAll] = useState(false);
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const [clock, setClock] = useState(0);

  const currency = summary?.workspace.base_currency ?? "INR";

  /* A visible clock while a run is in flight. A verification takes 60–120 seconds and
     a button that simply looks busy for two minutes reads as a hang. */
  useEffect(() => {
    if (startedAt === null) return;
    const timer = window.setInterval(() => setClock(Date.now() - startedAt), 500);
    return () => window.clearInterval(timer);
  }, [startedAt]);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(null), 6000);
    return () => window.clearTimeout(timer);
  }, [toast]);

  const openNode = useCallback((key: NodeKey) => {
    setSelected(key);
    setShowAdd(false);
    if (key === "evidence") setShowSources(true);
  }, []);

  /** Run one node. The backend decides whether it may. */
  const runNode = useCallback(
    async (key: NodeKey) => {
      if (key === "evidence") {
        setSelected("evidence");
        setShowSources(true);
        return;
      }
      if (key === "review") {
        setSelected("review");
        setToast({
          tone: "warn",
          text: "Human Review is settled by a person — open the decisions in the inspector.",
        });
        return;
      }
      const def = NODE_BY_KEY[key];
      if (!def.stage) return;

      graph.markRunning([key], true);
      setStartedAt(Date.now());
      const began = Date.now();
      try {
        const result = await api.runPipeline(workspaceId, [def.stage]);
        if (result.blocked) {
          setToast({ tone: "warn", text: result.remedy ?? result.blocked });
          graph.recordOutcome(key, (Date.now() - began) / 1000, result.blocked);
        } else {
          const stage = result.stages.find((s) => s.key === def.stage);
          const failed = stage?.status === "failed";
          graph.recordOutcome(
            key,
            stage?.seconds ?? (Date.now() - began) / 1000,
            failed ? stage?.detail || "The stage failed" : null,
          );
          if (!failed) {
            graph.clearStale([key]);
            graph.markStaleDownstream(key);
          }
          graph.setLastRunId(result.run_id);
        }
      } catch (err) {
        graph.recordOutcome(
          key,
          (Date.now() - began) / 1000,
          err instanceof ApiError ? err.message : "The stage failed",
        );
      } finally {
        graph.markRunning([key], false);
        setStartedAt(null);
        await graph.refresh();
      }
    },
    [graph, workspaceId],
  );

  /** Run everything. Missing nodes appear on the canvas as the backend reports them. */
  const runAll = useCallback(async () => {
    if (!graph.hasEvidence) {
      setToast({ tone: "warn", text: "Load evidence first — there is nothing to verify." });
      setSelected("evidence");
      setShowSources(true);
      return;
    }
    setRunningAll(true);
    setStartedAt(Date.now());
    // Every node the chain will touch joins the canvas up front, so the graph builds
    // itself in view rather than appearing all at once when the run returns.
    const stageKeys = NODES.filter((n) => n.stage).map((n) => n.key);
    graph.setAdded((prev) => new Set([...prev, ...stageKeys]));
    graph.markRunning(stageKeys, true);
    try {
      const result = await api.runPipeline(workspaceId);
      if (result.blocked) {
        setToast({ tone: "warn", text: result.remedy ?? result.blocked });
      } else {
        for (const stage of result.stages) {
          const node = NODES.find((n) => n.stage === stage.key);
          if (!node) continue;
          graph.recordOutcome(
            node.key,
            stage.seconds,
            stage.status === "failed" ? stage.detail || "The stage failed" : null,
          );
        }
        graph.setLastRunId(result.run_id);
        graph.clearStale(stageKeys);
        setToast({
          tone: result.stages_failed > 0 ? "error" : "success",
          text:
            result.stages_failed > 0
              ? `${result.stages_failed} of ${result.stages.length} stages failed.`
              : `All ${result.stages_run} stages completed in ${Math.round(result.seconds)}s.`,
        });
      }
    } catch (err) {
      setToast({
        tone: "error",
        text: err instanceof ApiError ? err.message : "The run could not be started",
      });
    } finally {
      graph.markRunning(stageKeys, false);
      setRunningAll(false);
      setStartedAt(null);
      await graph.refresh();
    }
  }, [graph, workspaceId]);

  const download = useCallback(
    async (key: NodeKey) => {
      try {
        await api.downloadArtifact(workspaceId, DOWNLOAD_KEY[key]);
      } catch (err) {
        setToast({
          tone: "error",
          text: err instanceof ApiError ? err.message : "Could not build that file",
        });
      }
    },
    [workspaceId],
  );

  const buildData = useCallback(
    (key: NodeKey): VerificationNodeData => {
      const def = NODE_BY_KEY[key];
      const state = graph.nodes[key];
      const missing = def.needs.filter((need) => !graph.presentKeys.has(need));
      return {
        nodeKey: key,
        status: state.status,
        inCount: state.inCount,
        outCount: state.outCount,
        seconds: state.seconds,
        error: state.error,
        lockedReason:
          missing.length > 0
            ? `Needs ${missing.map((m) => NODE_BY_KEY[m].title).join(" and ")}`
            : state.status === "locked"
              ? `Run ${def.needs.map((m) => NODE_BY_KEY[m].title).join(" and ")} first`
              : null,
        selected: selected === key,
        onRun: runNode,
        onDownload: download,
        onOpen: openNode,
      };
    },
    [graph.nodes, graph.presentKeys, selected, runNode, download, openNode],
  );

  /**
   * React Flow owns node positions once a node exists; this only adds, removes and
   * refreshes `data`.
   *
   * Rebuilding the whole array each render and handing back `LAYOUT[key]` — the same
   * object every time — let React Flow write drag positions straight into the layout
   * constants. One bad write put a node at an enormous coordinate, `fitView` zoomed
   * out far enough to include it, and the entire graph collapsed to a speck of beige
   * in the corner: nine nodes present in the DOM and none of them findable.
   */
  const [flowNodes, setFlowNodes, onNodesChange] = useNodesState<VerificationNodeData>(
    [],
  );

  useEffect(() => {
    setFlowNodes((current) => {
      const existing = new Map(current.map((node) => [node.id, node]));
      return NODES.filter((def) => graph.nodes[def.key].onCanvas).map((def) => {
        const prior = existing.get(def.key);
        const data = buildData(def.key);
        if (prior) return { ...prior, data };
        return {
          id: def.key,
          type: "verification",
          // A fresh object, so nothing downstream can mutate the shared layout.
          position: { ...LAYOUT[def.key] },
          draggable: true,
          data,
        };
      });
    });
  }, [graph.nodes, buildData, setFlowNodes]);

  const flowEdges: Edge[] = useMemo(() => {
    const edges: Edge[] = [];
    for (const def of NODES) {
      if (!graph.nodes[def.key].onCanvas) continue;
      for (const need of def.needs) {
        if (!graph.nodes[need].onCanvas) continue;
        const source = graph.nodes[need];
        const target = graph.nodes[def.key];
        const active = target.status === "running";
        const done = ["complete", "waiting", "stale"].includes(source.status);
        edges.push({
          id: `${need}->${def.key}`,
          source: need,
          target: def.key,
          type: "smoothstep",
          animated: false,
          label: NODE_BY_KEY[need].emits,
          labelShowBg: true,
          className: active ? "edge-running" : undefined,
          style: {
            stroke: active ? "#2563eb" : done ? "#047857" : "#cbd5e1",
            strokeWidth: active || done ? 1.75 : 1.25,
            strokeDasharray: done || active ? undefined : "4 4",
          },
          markerEnd: {
            type: MarkerType.ArrowClosed,
            width: 14,
            height: 14,
            color: active ? "#2563eb" : done ? "#047857" : "#cbd5e1",
          },
        });
      }
    }
    return edges;
  }, [graph.nodes]);

  /* While Run all is in flight, the newest event carrying a feature number tells us
     which stage is actually working. Marking only that one as running — rather than
     all seven — is the difference between a progress display and a spinner farm. */
  useEffect(() => {
    if (!runningAll) return;
    for (let i = events.length - 1; i >= 0; i -= 1) {
      const key = events[i].feature ? FEATURE_TO_NODE[events[i].feature!] : undefined;
      if (!key || key === "evidence") continue;
      const order = NODES.filter((n) => n.stage).map((n) => n.key);
      const at = order.indexOf(key);
      if (at < 0) continue;
      markRunning(order.slice(at + 1), false);
      markRunning([key], true);
      break;
    }
  }, [events, runningAll, markRunning]);

  const latest = events.length > 0 ? events[events.length - 1].message : null;
  const selectedState = selected ? graph.nodes[selected] : null;
  const evidenceAnchor = useRef<HTMLDivElement>(null);

  return (
    <div className="flex h-full min-h-0">
      <div className="relative flex min-w-0 flex-1 flex-col">
        {/* --- canvas toolbar ------------------------------------------------ */}
        <div className="flex items-center gap-2 border-b border-line bg-paper px-4 py-2">
          <div className="relative">
            <Button onClick={() => setShowAdd((v) => !v)} icon={<PlusIcon />}>
              Add node
            </Button>
            {showAdd && (
              <div className="absolute left-0 top-full z-30 mt-1.5">
                <AddNodePanel
                  present={graph.presentKeys}
                  onAdd={(key) => {
                    const refusal = graph.addNode(key);
                    if (!refusal) setSelected(key);
                    return refusal;
                  }}
                  onClose={() => setShowAdd(false)}
                />
              </div>
            )}
          </div>

          <div className="ml-auto flex items-center gap-2">
            {startedAt !== null && (
              <span className="flex items-center gap-2 rounded-full bg-cobalt-soft px-3 py-1 text-[12px] text-cobalt">
                <Spinner />
                Verification running
                <span className="font-mono tabular-nums">{elapsedClock(clock)}</span>
              </span>
            )}
            <Button
              onClick={() => void api.downloadBundle(workspaceId)}
              disabled={!graph.hasEvidence}
              icon={<DownloadAllIcon />}
            >
              Download all
            </Button>
            <Button
              variant="primary"
              onClick={() => void runAll()}
              disabled={runningAll}
              icon={runningAll ? <Spinner /> : <PlayIcon />}
            >
              {runningAll ? "Running…" : "Run all"}
            </Button>
          </div>
        </div>

        {!graph.hasEvidence && !graph.loading && (
          <div className="absolute left-1/2 top-[76px] z-20 w-[430px] -translate-x-1/2">
            <Banner tone="info">
              <strong className="font-semibold">Start here.</strong> Load evidence into
              node 01, then <strong className="font-semibold">Run all</strong> builds and
              runs the rest of the graph for you.
            </Banner>
          </div>
        )}

        {toast && (
          <div className="absolute left-1/2 top-[76px] z-30 w-[430px] -translate-x-1/2">
            <Banner tone={toast.tone}>{toast.text}</Banner>
          </div>
        )}

        {/* --- the canvas ---------------------------------------------------- */}
        <div className="relative min-h-0 flex-1" ref={evidenceAnchor}>
          <ReactFlowProvider>
            <ReactFlow
              nodes={flowNodes}
              edges={flowEdges}
              onNodesChange={onNodesChange}
              nodeTypes={NODE_TYPES}
              fitView
              fitViewOptions={{ padding: 0.2, maxZoom: 1, minZoom: 0.35 }}
              minZoom={0.35}
              maxZoom={1.5}
              proOptions={{ hideAttribution: true }}
              onPaneClick={() => {
                setShowAdd(false);
                setShowSources(false);
              }}
            >
              <FitOnGrowth count={flowNodes.length} />
              <Background
                variant={BackgroundVariant.Dots}
                gap={22}
                size={1.4}
                color="#dcd5c7"
              />
              <Controls
                showInteractive={false}
                className="!bottom-4 !left-4 !shadow-[0_1px_3px_rgba(15,23,42,0.1)]"
              />
              <MiniMap
                pannable
                zoomable
                className="!bottom-4 !right-4 !h-[86px] !w-[132px] !rounded-[8px] !border !border-line !bg-paper"
                maskColor="rgba(250,248,243,0.72)"
                nodeColor={(node) => {
                  const state = graph.nodes[node.id as NodeKey];
                  if (!state) return "#cbd5e1";
                  const swatch: Partial<Record<NodeStatus, string>> = {
                    complete: "#047857",
                    running: "#2563eb",
                    failed: "#b42318",
                    waiting: "#b45309",
                    stale: "#b45309",
                    required: "#b45309",
                  };
                  return swatch[state.status] ?? "#cbd5e1";
                }}
              />
            </ReactFlow>
          </ReactFlowProvider>

          {showSources && (
            <div className="absolute left-1/2 top-1/2 z-30 -translate-x-1/2 -translate-y-1/2">
              <SourcePicker
                workspaceId={workspaceId}
                connections={summary?.connections ?? []}
                onClose={() => setShowSources(false)}
                onLoaded={() => {
                  void graph.refresh();
                  setToast({ tone: "success", text: "Evidence loaded. Run all is ready." });
                  setShowSources(false);
                }}
              />
            </div>
          )}
        </div>

        <ExecutionDrawer
          nodes={graph.nodes}
          open={drawerOpen}
          onToggle={() => setDrawerOpen((v) => !v)}
          onSelect={openNode}
          currentLog={latest}
          runId={graph.lastRunId}
        />
      </div>

      {selectedState && (
        <Inspector
          key={selectedState.key}
          workspaceId={workspaceId}
          node={selectedState}
          events={events}
          streaming={streaming}
          currency={currency}
          onClose={() => setSelected(null)}
          onRun={runNode}
          onDownload={download}
          onResolved={() => void graph.refresh()}
        />
      )}
    </div>
  );
}

function PlusIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden>
      <path d="M8 3.4v9.2M3.4 8h9.2" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
    </svg>
  );
}

function DownloadAllIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden>
      <path
        d="M8 1.8v7.6m0 0 2.8-2.8M8 9.4 5.2 6.6M2.4 11.4v1.6c0 .6.5 1.1 1.1 1.1h9c.6 0 1.1-.5 1.1-1.1v-1.6"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
