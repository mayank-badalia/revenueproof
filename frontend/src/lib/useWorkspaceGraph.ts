"use client";

/**
 * The canvas's state: which nodes are on it, what each one has done, and what it is
 * allowed to do next.
 *
 * Everything here is *derived from the backend*. `has_run` comes from counting real
 * rows (pipeline.evidence_state), the in/out figures are real record counts, and a
 * refusal to run comes back from the API rather than being predicted here. The
 * frontend decides only what to show and when to grey a button — never whether a
 * figure is true.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { API_BASE, api } from "@/lib/api";
import {
  NODES,
  NODE_BY_KEY,
  type NodeKey,
  type NodeStatus,
  unmetPrerequisites,
  downstreamOf,
} from "@/lib/graph";
import type { PipelineState, ReviewQueue, WorkspaceSummary } from "@/lib/types";

export interface NodeState {
  key: NodeKey;
  onCanvas: boolean;
  status: NodeStatus;
  /** Records read, records produced. Blank where the node has not run. */
  inCount: number | null;
  outCount: number | null;
  detail: string;
  seconds: number | null;
  error: string | null;
  /** Set when an upstream node has re-run since this one last did. */
  stale: boolean;
}

export interface GraphSnapshot {
  nodes: Record<NodeKey, NodeState>;
  hasEvidence: boolean;
  counts: Record<string, number>;
  openDecisions: number;
  loading: boolean;
  /** Present once a run has happened, so the canvas can show durations. */
  lastRunId: string | null;
}

const EMPTY: NodeState = {
  key: "evidence",
  onCanvas: false,
  status: "locked",
  inCount: null,
  outCount: null,
  detail: "",
  seconds: null,
  error: null,
  stale: false,
};

/** Which record counts stand for a node's input and output. */
function countsFor(
  key: NodeKey,
  counts: Record<string, number>,
  pipeline: PipelineState | null,
  openDecisions: number,
  resolvedDecisions: number,
): { inCount: number | null; outCount: number | null } {
  const c = (name: string) => counts[name] ?? 0;
  switch (key) {
    case "evidence":
      return { inCount: null, outCount: pipeline?.raw_records ?? c("raw_records") };
    case "identity":
      return { inCount: pipeline?.raw_records ?? c("raw_records"), outCount: c("customers") };
    case "contracts":
      return { inCount: c("contracts"), outCount: c("contracts") };
    case "reconcile":
      return { inCount: c("invoices") + c("payments"), outCount: c("allocations") };
    case "revenue":
      return { inCount: c("allocations"), outCount: c("revenue_items") };
    case "anomalies":
      return { inCount: c("revenue_items"), outCount: c("anomalies") };
    case "critic":
      return { inCount: c("revenue_items"), outCount: c("critic_decisions") };
    case "review":
      return { inCount: openDecisions, outCount: resolvedDecisions };
    case "publish":
      return { inCount: c("revenue_items"), outCount: c("report_versions") };
  }
}

export function useWorkspaceGraph(workspaceId: string) {
  const [pipeline, setPipeline] = useState<PipelineState | null>(null);
  const [summary, setSummary] = useState<WorkspaceSummary | null>(null);
  const [queue, setQueue] = useState<ReviewQueue | null>(null);
  const [loading, setLoading] = useState(true);

  /* Nodes the user has explicitly added but which have not run yet. A node that has
     run is on the canvas by definition; this set is only for the ones that are not
     yet backed by any rows. */
  const [added, setAdded] = useState<Set<NodeKey>>(new Set(["evidence"]));
  const [running, setRunning] = useState<Set<NodeKey>>(new Set());
  const [failed, setFailed] = useState<Record<string, string>>({});
  const [durations, setDurations] = useState<Record<string, number>>({});
  const [staleKeys, setStaleKeys] = useState<Set<NodeKey>>(new Set());
  const [lastRunId, setLastRunId] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    const [p, s, q] = await Promise.allSettled([
      api.pipelineState(workspaceId),
      api.workspaceSummary(workspaceId),
      api.listReview(workspaceId),
    ]);
    if (p.status === "fulfilled") setPipeline(p.value);
    if (s.status === "fulfilled") setSummary(s.value);
    if (q.status === "fulfilled") setQueue(q.value);
    setLoading(false);
  }, [workspaceId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const openDecisions = queue?.summary.open_decisions ?? summary?.open_review_items ?? 0;
  const resolvedDecisions = queue?.summary.resolved ?? 0;
  const counts = useMemo(() => summary?.evidence_counts ?? {}, [summary]);
  const hasEvidence = pipeline?.has_evidence ?? false;

  const nodes = useMemo(() => {
    const completed = new Set<NodeKey>();
    if (hasEvidence) completed.add("evidence");
    for (const [key, done] of Object.entries(pipeline?.completed ?? {})) {
      if (done) completed.add(key as NodeKey);
    }
    // Human Review is not a pipeline stage: it is complete when nothing is open, and
    // it only earns a place on the canvas once the critic has produced something to
    // argue about.
    const criticRan = completed.has("critic");

    const out = {} as Record<NodeKey, NodeState>;
    for (const def of NODES) {
      const key = def.key;
      const hasRun = key === "review" ? criticRan && openDecisions === 0 : completed.has(key);
      const onCanvas =
        added.has(key) ||
        completed.has(key) ||
        (key === "review" && criticRan && openDecisions > 0);

      const unmet = unmetPrerequisites(key, completed);
      let status: NodeStatus;
      if (running.has(key)) status = "running";
      else if (failed[key]) status = "failed";
      else if (key === "evidence") status = hasEvidence ? "complete" : "required";
      else if (key === "review")
        status = !criticRan ? "locked" : openDecisions > 0 ? "waiting" : "complete";
      else if (hasRun) status = staleKeys.has(key) ? "stale" : "complete";
      else if (unmet.length > 0) status = "locked";
      else status = "ready";

      const { inCount, outCount } = countsFor(
        key,
        counts,
        pipeline,
        openDecisions,
        resolvedDecisions,
      );
      out[key] = {
        ...EMPTY,
        key,
        onCanvas,
        status,
        // "Waiting on you" is a node that has done its work and is holding a
        // number, so it shows its figures like any other. Only a node that has not
        // run yet shows nothing.
        inCount: hasRun || status === "running" || status === "waiting" ? inCount : null,
        outCount: hasRun || status === "waiting" ? outCount : null,
        detail: "",
        seconds: durations[key] ?? null,
        error: failed[key] ?? null,
        stale: staleKeys.has(key),
      };
    }
    return out;
  }, [
    pipeline,
    counts,
    openDecisions,
    resolvedDecisions,
    added,
    running,
    failed,
    durations,
    staleKeys,
    hasEvidence,
  ]);

  const presentKeys = useMemo(
    () => new Set(NODES.filter((n) => nodes[n.key].onCanvas).map((n) => n.key)),
    [nodes],
  );

  /** Put a node on the canvas. Refuses duplicates and unmet prerequisites. */
  const addNode = useCallback(
    (key: NodeKey): string | null => {
      if (presentKeys.has(key)) return "Already added";
      const missing = NODE_BY_KEY[key].needs.filter((need) => !presentKeys.has(need));
      if (missing.length > 0) {
        return `Add ${missing.map((k) => NODE_BY_KEY[k].title).join(" and ")} first`;
      }
      setAdded((prev) => new Set(prev).add(key));
      return null;
    },
    [presentKeys],
  );

  const markRunning = useCallback((keys: NodeKey[], on: boolean) => {
    setRunning((prev) => {
      // Returning a fresh Set unconditionally made every call a state change, and
      // the trace effect that calls this re-fires on state change — a render loop
      // that React stops with "maximum update depth exceeded". Identical input must
      // return the identical object.
      const changed = keys.some((key) => prev.has(key) !== on);
      if (!changed) return prev;
      const next = new Set(prev);
      for (const key of keys) {
        if (on) next.add(key);
        else next.delete(key);
      }
      return next;
    });
  }, []);

  /** Re-running a node invalidates everything computed from its output. */
  const markStaleDownstream = useCallback((key: NodeKey) => {
    setStaleKeys((prev) => {
      const next = new Set(prev);
      next.delete(key);
      for (const child of downstreamOf(key)) next.add(child);
      return next;
    });
  }, []);

  const clearStale = useCallback((keys: NodeKey[]) => {
    setStaleKeys((prev) => {
      const next = new Set(prev);
      for (const key of keys) next.delete(key);
      return next;
    });
  }, []);

  const recordOutcome = useCallback(
    (key: NodeKey, seconds: number, error: string | null) => {
      setDurations((prev) => ({ ...prev, [key]: seconds }));
      setFailed((prev) => {
        const next = { ...prev };
        if (error) next[key] = error;
        else delete next[key];
        return next;
      });
    },
    [],
  );

  /*
   * Memoised deliberately. A fresh object literal here is a new identity on every
   * render, and anything that lists the hook's result as an effect dependency then
   * re-runs forever — which is exactly what happened: the canvas's node-sync effect
   * fired on every render, set state, rendered again, and React halted the page with
   * "maximum update depth exceeded".
   */
  return useMemo(
    () => ({
      nodes,
      presentKeys,
      hasEvidence,
      counts,
      openDecisions,
      loading,
      lastRunId,
      setLastRunId,
      summary,
      queue,
      refresh,
      addNode,
      markRunning,
      markStaleDownstream,
      clearStale,
      recordOutcome,
      setAdded,
    }),
    [
      nodes,
      presentKeys,
      hasEvidence,
      counts,
      openDecisions,
      loading,
      lastRunId,
      summary,
      queue,
      refresh,
      addNode,
      markRunning,
      markStaleDownstream,
      clearStale,
      recordOutcome,
    ],
  );
}

export interface TraceLine {
  id: string;
  at: string;
  message: string;
  kind: string;
  severity: string;
  /** The feature number the backend stamped on this event, where it stamped one. */
  feature: number | null;
}

/**
 * The backend stamps each event with the feature that emitted it, which is the only
 * live signal of *which* stage is working. `/pipeline/run` is a single blocking call
 * that returns once everything is done, so without this the canvas could only show
 * all seven nodes spinning at once and then all seven finishing at once — true, and
 * useless to watch.
 */
export const FEATURE_TO_NODE: Record<number, NodeKey> = {
  1: "evidence",
  2: "identity",
  3: "contracts",
  4: "reconcile",
  5: "revenue",
  6: "anomalies",
  7: "critic",
  8: "publish",
};

/** Live trace events for this workspace, over SSE. */
export function useTrace(workspaceId: string, limit = 60) {
  const [events, setEvents] = useState<TraceLine[]>([]);
  const [streaming, setStreaming] = useState(false);
  const seen = useRef(new Set<string>());

  useEffect(() => {
    seen.current = new Set();
    let source: EventSource | null = null;
    let cancelled = false;

    const push = (raw: Record<string, unknown>) => {
      const id = String(raw.event_id ?? Math.random());
      if (seen.current.has(id)) return;
      seen.current.add(id);
      setEvents((prev) =>
        [
          ...prev,
          {
            id,
            at: String(raw.timestamp ?? ""),
            message: String(raw.message ?? ""),
            kind: String(raw.kind ?? "system"),
            severity: String(raw.severity ?? "info"),
            feature: typeof raw.feature === "number" ? raw.feature : null,
          },
        ].slice(-limit),
      );
    };

    (async () => {
      // Cleared here rather than in the effect body: a synchronous reset is a
      // cascading render, and the history fetched below replaces it anyway.
      if (!cancelled) setEvents([]);
      try {
        const history = await api.recentEvents(workspaceId);
        if (cancelled) return;
        for (const event of history.events.slice(-limit)) {
          push(event as unknown as Record<string, unknown>);
        }
      } catch {
        /* History is a convenience; the stream below is the live signal. */
      }

      const token =
        typeof window === "undefined"
          ? null
          : window.localStorage.getItem("revenueproof.token");
      if (!token || cancelled) return;

      source = new EventSource(
        `${API_BASE}/api/v1/events/stream/${workspaceId}?token=${encodeURIComponent(token)}`,
      );
      source.onopen = () => setStreaming(true);
      source.onerror = () => setStreaming(false);
      source.onmessage = (message) => {
        try {
          const payload = JSON.parse(message.data);
          if (payload.type === "event" && payload.event) push(payload.event);
        } catch {
          /* A malformed frame is not worth breaking the panel over. */
        }
      };
    })();

    return () => {
      cancelled = true;
      source?.close();
      setStreaming(false);
    };
  }, [workspaceId, limit]);

  return { events, streaming };
}
