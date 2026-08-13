"use client";

/**
 * Live processing trace (§10.3).
 *
 * Mirrors the Python terminal in the browser: every API call, agent step, tool
 * call, rule evaluation and error, as it happens. This is an operational trace —
 * actions, evidence and status — not model chain-of-thought.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { api, openTraceSocket } from "@/lib/api";
import type { TraceEvent } from "@/lib/types";

const KIND_LABEL: Record<TraceEvent["kind"], string> = {
  system: "SYS",
  api_call: "API",
  agent_step: "AGENT",
  tool_call: "TOOL",
  rule: "RULE",
  persistence: "DB",
  test: "TEST",
  error: "ERROR",
  result: "RESULT",
  progress: "PROG",
};

const SEVERITY_STYLE: Record<TraceEvent["severity"], string> = {
  debug: "text-slate-400",
  info: "text-sky-300",
  success: "text-emerald-300",
  warning: "text-amber-300",
  error: "text-rose-300",
};

const KIND_BADGE: Record<TraceEvent["kind"], string> = {
  system: "bg-slate-700 text-slate-200",
  api_call: "bg-sky-900 text-sky-200",
  agent_step: "bg-violet-900 text-violet-200",
  tool_call: "bg-indigo-900 text-indigo-200",
  rule: "bg-teal-900 text-teal-200",
  persistence: "bg-slate-700 text-slate-200",
  test: "bg-lime-900 text-lime-200",
  error: "bg-rose-900 text-rose-200",
  result: "bg-emerald-900 text-emerald-200",
  progress: "bg-amber-900 text-amber-200",
};

// Bounded so a long verification run cannot grow the DOM without limit.
const MAX_EVENTS = 600;

export function TraceViewer({ workspaceId }: { workspaceId: string }) {
  const [events, setEvents] = useState<TraceEvent[]>([]);
  const [status, setStatus] = useState<"connecting" | "open" | "closed">("connecting");
  const [autoScroll, setAutoScroll] = useState(true);
  // Debug lines are on by default. They are the ones that say which model was
  // called, with what, and how long it took — the detail someone watching a run
  // actually wants — and making them opt-in meant the trace looked sparse to
  // everyone who did not know the checkbox existed.
  const [showDebug, setShowDebug] = useState(true);
  const [expanded, setExpanded] = useState<string | null>(null);
  const logRef = useRef<HTMLDivElement>(null);

  const appendEvent = useCallback((event: TraceEvent) => {
    setEvents((current) => {
      // The socket replays history on connect, so guard against duplicates
      // after a reconnect.
      if (current.some((e) => e.event_id === event.event_id)) return current;
      const next = [...current, event];
      return next.length > MAX_EVENTS ? next.slice(-MAX_EVENTS) : next;
    });
  }, []);

  useEffect(() => {
    // Seed from HTTP so the panel is populated even if the socket is blocked.
    api
      .recentEvents(workspaceId)
      .then((response) => setEvents(response.events.slice(-MAX_EVENTS)))
      .catch(() => undefined);

    return openTraceSocket(workspaceId, appendEvent, setStatus);
  }, [workspaceId, appendEvent]);

  useEffect(() => {
    if (!autoScroll) return;
    // Scroll the log's own box, never the page. `scrollIntoView` on a child moves
    // the *document* to bring that child into view, so every new trace line yanked
    // the reader back down to the trace panel from wherever they were working —
    // in the middle of reading a finding, or halfway through a review decision.
    // A panel that follows its own output must not move anything outside itself.
    const box = logRef.current;
    if (box) box.scrollTop = box.scrollHeight;
  }, [events, autoScroll]);

  const visible = showDebug ? events : events.filter((e) => e.severity !== "debug");

  return (
    <section className="rounded-lg border border-slate-200 bg-white shadow-sm">
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 px-4 py-3">
        <div className="flex items-center gap-2">
          <h2 className="text-sm font-semibold">Processing trace</h2>
          <span
            className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium ${
              status === "open"
                ? "bg-emerald-100 text-emerald-800"
                : status === "connecting"
                  ? "bg-amber-100 text-amber-800"
                  : "bg-rose-100 text-rose-800"
            }`}
          >
            <span
              className={`h-1.5 w-1.5 rounded-full ${
                status === "open"
                  ? "bg-emerald-500"
                  : status === "connecting"
                    ? "bg-amber-500"
                    : "bg-rose-500"
              }`}
            />
            {status === "open" ? "live" : status}
          </span>
          <span className="text-xs text-slate-500">{visible.length} events</span>
        </div>

        <div className="flex items-center gap-4 text-xs">
          <label className="flex items-center gap-1.5 text-slate-600">
            <input
              type="checkbox"
              checked={showDebug}
              onChange={(e) => setShowDebug(e.target.checked)}
              className="h-3.5 w-3.5"
            />
            debug
          </label>
          <label className="flex items-center gap-1.5 text-slate-600">
            <input
              type="checkbox"
              checked={autoScroll}
              onChange={(e) => setAutoScroll(e.target.checked)}
              className="h-3.5 w-3.5"
            />
            follow
          </label>
          <button
            type="button"
            onClick={() => setEvents([])}
            className="rounded border border-slate-300 px-2 py-1 text-slate-600 hover:bg-slate-50"
          >
            clear
          </button>
        </div>
      </header>

      <div
        ref={logRef}
        className="h-96 overflow-y-auto overscroll-contain bg-slate-900 p-3 font-mono text-xs leading-relaxed"
      >
        {visible.length === 0 ? (
          <p className="text-slate-500">
            No activity yet. Connect a source or start a verification run.
          </p>
        ) : (
          visible.map((event) => (
            <div key={event.event_id} className="mb-1">
              <button
                type="button"
                onClick={() =>
                  setExpanded(expanded === event.event_id ? null : event.event_id)
                }
                className="flex w-full items-start gap-2 text-left hover:bg-slate-800/60"
              >
                <span className="shrink-0 text-slate-500">
                  {event.timestamp.slice(11, 23)}
                </span>
                <span
                  className={`shrink-0 rounded px-1 text-[10px] font-semibold ${KIND_BADGE[event.kind]}`}
                >
                  {KIND_LABEL[event.kind]}
                </span>
                {event.feature !== null && (
                  <span className="shrink-0 text-slate-500">F{event.feature}</span>
                )}
                <span className={`flex-1 break-words ${SEVERITY_STYLE[event.severity]}`}>
                  {event.message}
                </span>
                {event.duration_ms !== null && (
                  <span className="shrink-0 text-slate-500">
                    {event.duration_ms.toFixed(0)}ms
                  </span>
                )}
              </button>
              {expanded === event.event_id && Object.keys(event.data).length > 0 && (
                <pre className="mt-1 ml-6 overflow-x-auto rounded bg-slate-950 p-2 text-[11px] text-slate-300">
                  {JSON.stringify(event.data, null, 2)}
                </pre>
              )}
            </div>
          ))
        )}
      </div>
    </section>
  );
}
