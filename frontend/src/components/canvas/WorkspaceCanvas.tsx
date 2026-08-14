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
  bypassedBetween,
  nearestPresentSources,
  type NodeKey,
  type NodeStatus,
} from "@/lib/graph";
import { FEATURE_TO_NODE, useTrace, useWorkspaceGraph } from "@/lib/useWorkspaceGraph";
import { loadLayout, saveLayout } from "@/lib/canvasStore";
import { RunPreflight } from "./RunPreflight";
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
  const [cutting, setCutting] = useState(false);
  const [preflight, setPreflight] = useState<null | {
    required: NodeKey[];
    recommended: NodeKey[];
    disconnected: { key: NodeKey; needs: NodeKey[] }[];
  }>(null);
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

  const openNode = useCallback(
    (key: NodeKey) => {
      // With the scissors up, a click cuts rather than opens. Cutting stays on so a
      // reviewer can remove several without going back to the toolbar each time.
      if (cutting) {
        const refusal = graph.removeNode(key);
        setToast(
          refusal
            ? { tone: "warn", text: refusal }
            : { tone: "success", text: `Cut ${NODE_BY_KEY[key].title} — ⌘Z to undo` },
        );
        if (!refusal && selected === key) setSelected(null);
        return;
      }
      setSelected(key);
      setShowAdd(false);
      if (key === "evidence") setShowSources(true);
    },
    [cutting, graph, selected],
  );

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
  /** Actually start the chain. `runAll` checks the canvas first and calls this. */
  const startRun = useCallback(async () => {
    setPreflight(null);
    setRunningAll(true);
    setStartedAt(Date.now());
    // Every node the chain will touch joins the canvas up front, so the graph builds
    // itself in view rather than appearing all at once when the run returns.
    const stageKeys = NODES.filter((n) => n.stage && graph.presentKeys.has(n.key)).map(
      (n) => n.key,
    );
    graph.markRunning(stageKeys, true);
    try {
      const stages = NODES.filter(
        (n) => n.stage && graph.presentKeys.has(n.key),
      ).map((n) => n.stage!);
      const result = await api.runPipeline(
        workspaceId,
        stages.length === NODES.filter((n) => n.stage).length ? undefined : stages,
      );
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

  /**
   * Check the canvas before running. A removed node is a real choice, but pressing Run
   * afterwards must not quietly return a weaker answer that looks like a strong one.
   */
  const runAll = useCallback(() => {
    if (!graph.hasEvidence) {
      setToast({ tone: "warn", text: "Load evidence first — there is nothing to verify." });
      setSelected("evidence");
      setShowSources(true);
      return;
    }
    const missing = NODES.filter((n) => !graph.presentKeys.has(n.key));
    const required = missing.filter((n) => n.importance === "required").map((n) => n.key);
    const recommended = missing
      .filter((n) => n.importance === "recommended" && n.key !== "review")
      .map((n) => n.key);
    const disconnected = NODES.filter((n) => graph.presentKeys.has(n.key))
      .map((n) => ({
        key: n.key,
        needs: n.needs.filter((need) => !graph.presentKeys.has(need)),
      }))
      .filter((n) => n.needs.length > 0);

    if (required.length || recommended.length || disconnected.length) {
      setPreflight({ required, recommended, disconnected });
      return;
    }
    void startRun();
  }, [graph, startRun]);

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

  const saved = useMemo(() => loadLayout(workspaceId), [workspaceId]);

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
          // A saved arrangement wins over the default one, and both are cloned so
          // nothing downstream can mutate the shared layout constants.
          position: { ...(saved.positions[def.key] ?? LAYOUT[def.key]) },
          draggable: true,
          data,
        };
      });
    });
  }, [graph.nodes, buildData, setFlowNodes, saved]);

  /*
   * While the scissors are up, nothing drags.
   *
   * React Flow reads a press-and-move as a drag rather than a click, so a cut aimed at
   * a node that moved two pixels under the cursor silently repositioned it instead —
   * which reads exactly like the cut tool not working.
   */
  useEffect(() => {
    setFlowNodes((current) =>
      current.map((node) =>
        node.draggable === !cutting ? node : { ...node, draggable: !cutting },
      ),
    );
  }, [cutting, setFlowNodes]);

  /** Where a reviewer put a node is a decision about how they read this graph. */
  const persistPositions = useCallback(
    (nodes: { id: string; position: { x: number; y: number } }[]) => {
      const existing = loadLayout(workspaceId);
      const positions = { ...existing.positions };
      for (const node of nodes) positions[node.id as NodeKey] = { ...node.position };
      saveLayout(workspaceId, { ...existing, positions });
    },
    [workspaceId],
  );

  /* Undo. Deleting a node is one keystroke and rebuilding the arrangement by hand is
     many, so the shortcut everyone already has in their fingers has to work. */
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      const target = event.target as HTMLElement | null;
      if (target && /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)) return;
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "z") {
        event.preventDefault();
        const label = graph.undoLast();
        if (label) setToast({ tone: "success", text: `Undone — ${label.toLowerCase()}` });
      }
      if (event.key === "Escape") setCutting(false);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [graph]);

  const flowEdges: Edge[] = useMemo(() => {
    const edges: Edge[] = [];
    const seen = new Set<string>();

    for (const def of NODES) {
      if (!graph.nodes[def.key].onCanvas) continue;

      for (const need of def.needs) {
        // A cut node does not orphan what came after it: the edge reaches back to the
        // nearest node still present, and is drawn as a bypass so the skipped step is
        // visible rather than silently forgotten.
        const sources = nearestPresentSources(need, graph.presentKeys);
        for (const sourceKey of sources) {
          const id = `${sourceKey}->${def.key}`;
          if (seen.has(id)) continue;
          seen.add(id);

          const bypassed = bypassedBetween(sourceKey, need, graph.presentKeys);
          const isBypass = bypassed.length > 0;
          const source = graph.nodes[sourceKey];
          const target = graph.nodes[def.key];
          const active = target.status === "running";
          const done = ["complete", "waiting", "stale"].includes(source.status);

          const colour = isBypass
            ? "#b45309"
            : active
              ? "#2563eb"
              : done
                ? "#047857"
                : "#cbd5e1";

          edges.push({
            id,
            source: sourceKey,
            target: def.key,
            type: "smoothstep",
            animated: false,
            label: isBypass
              ? `skips ${bypassed.map((k) => NODE_BY_KEY[k].title).join(", ")}`
              : NODE_BY_KEY[sourceKey].emits,
            labelShowBg: true,
            className: active && !isBypass ? "edge-running" : undefined,
            style: {
              stroke: colour,
              strokeWidth: isBypass ? 1.5 : active || done ? 1.75 : 1.25,
              strokeDasharray: isBypass ? "6 4" : done || active ? undefined : "4 4",
            },
            labelStyle: isBypass ? { fill: "#b45309", fontWeight: 500 } : undefined,
            markerEnd: {
              type: MarkerType.ArrowClosed,
              width: 14,
              height: 14,
              color: colour,
            },
          });
        }
      }
    }
    return edges;
  }, [graph.nodes, graph.presentKeys]);

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
                  removed={graph.removedKeys}
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

          <Button
            onClick={() => setCutting((v) => !v)}
            icon={<ScissorsIcon />}
            className={cutting ? "!bg-rust !text-white ring-0" : ""}
            title="Cut a node off the canvas — click a node while this is on. Esc to stop."
          >
            {cutting ? "Cutting — click a node" : "Cut"}
          </Button>

          {graph.canUndo && (
            <Button
              onClick={() => {
                const label = graph.undoLast();
                if (label) setToast({ tone: "success", text: `Undone — ${label.toLowerCase()}` });
              }}
              icon={<UndoIcon />}
              title="Undo (⌘Z)"
            >
              Undo
            </Button>
          )}

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
              onClick={runAll}
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
        <div
          className={`relative min-h-0 flex-1 ${cutting ? "canvas-cutting" : ""}`}
          ref={evidenceAnchor}
        >
          <ReactFlowProvider>
            <ReactFlow
              nodes={flowNodes}
              edges={flowEdges}
              onNodesChange={onNodesChange}
              onNodeDragStop={(_, __, dragged) => persistPositions(dragged)}
              onNodesDelete={(deleted) => {
                for (const node of deleted) {
                  const refusal = graph.removeNode(node.id as NodeKey);
                  if (refusal) setToast({ tone: "warn", text: refusal });
                  else if (selected === node.id) setSelected(null);
                }
              }}
              deleteKeyCode={["Backspace", "Delete"]}
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
                deploymentProviders={summary?.deployment_providers}
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

      {preflight && (
        <RunPreflight
          missingRequired={preflight.required}
          missingRecommended={preflight.recommended}
          disconnected={preflight.disconnected}
          onCancel={() => setPreflight(null)}
          onRunAnyway={() => void startRun()}
          onAddAndRun={() => {
            const restore = [
              ...preflight.required,
              ...preflight.recommended,
              ...preflight.disconnected.flatMap((d) => d.needs),
            ];
            for (const key of restore) graph.addNode(key);
            setPreflight(null);
            // Let the newly added nodes reach state before the run reads presence.
            window.setTimeout(() => void startRun(), 60);
          }}
        />
      )}

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
          counts={graph.counts}
          onResolved={() => void graph.refresh()}
          onRemove={(key) => {
            const refusal = graph.removeNode(key);
            if (refusal) setToast({ tone: "warn", text: refusal });
            else setSelected(null);
          }}
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

function ScissorsIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden>
      <circle cx="3.6" cy="12.2" r="2" stroke="currentColor" strokeWidth="1.4" />
      <circle cx="12.4" cy="12.2" r="2" stroke="currentColor" strokeWidth="1.4" />
      <path d="M4.9 10.9 12.4 2M11.1 10.9 3.6 2" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
    </svg>
  );
}

function UndoIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden>
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
