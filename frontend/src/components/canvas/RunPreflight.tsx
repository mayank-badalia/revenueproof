"use client";

/**
 * What is missing from the canvas, before a run rather than after it.
 *
 * Removing a node is a legitimate thing to do — but pressing Run afterwards should not
 * quietly produce a weaker answer that looks identical to a strong one. So a stage the
 * chain genuinely cannot do without is named as such and offered back; one that only
 * makes the result better says exactly what is lost by leaving it out, and the reviewer
 * decides.
 *
 * The same dialog covers a node whose upstream was removed: with edges derived from the
 * dependency graph, "not connected" and "its prerequisite is missing" are the same
 * condition, and the fix — put the upstream node back — is the same too.
 */

import { NODE_BY_KEY, type NodeKey } from "@/lib/graph";
import { Button } from "@/components/ui/primitives";

export function RunPreflight({
  missingRequired,
  missingRecommended,
  disconnected,
  onAddAndRun,
  onRunAnyway,
  onCancel,
}: {
  missingRequired: NodeKey[];
  missingRecommended: NodeKey[];
  /** On the canvas, but its prerequisite is not — so nothing feeds it. */
  disconnected: { key: NodeKey; needs: NodeKey[] }[];
  onAddAndRun: () => void;
  onRunAnyway: () => void;
  onCancel: () => void;
}) {
  const blocked = missingRequired.length > 0 || disconnected.length > 0;

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-navy-900/35 p-6 pt-[10vh]">
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Before running"
        className="w-full max-w-[480px] rounded-[12px] border border-line bg-paper shadow-[0_20px_60px_-12px_rgba(8,17,31,0.28)]"
      >
        <header className="border-b border-line px-5 py-4">
          <h2 className="text-[15px] font-semibold tracking-[-0.01em] text-ink">
            {blocked ? "Some nodes the run needs are missing" : "Running without some nodes"}
          </h2>
          <p className="mt-0.5 text-[12.5px] leading-relaxed text-ink-2">
            {blocked
              ? "You removed these from the canvas. The chain cannot reach a published figure without them."
              : "You removed these. The run will finish without them, but the result is weaker in the ways below."}
          </p>
        </header>

        <div className="max-h-[46vh] space-y-3 overflow-y-auto scroll-thin px-5 py-4">
          {missingRequired.length > 0 && (
            <section>
              <h3 className="text-[10.5px] font-semibold uppercase tracking-[0.09em] text-rust">
                Required
              </h3>
              <div className="mt-1.5 space-y-1.5">
                {missingRequired.map((key) => (
                  <MissingRow key={key} nodeKey={key} tone="bad" />
                ))}
              </div>
            </section>
          )}

          {disconnected.length > 0 && (
            <section>
              <h3 className="text-[10.5px] font-semibold uppercase tracking-[0.09em] text-amber">
                Nothing feeding them
              </h3>
              <div className="mt-1.5 space-y-1.5">
                {disconnected.map(({ key, needs }) => (
                  <div
                    key={key}
                    className="relative overflow-hidden rounded-[7px] border border-line px-2.5 py-2 pl-3"
                  >
                    <span className="absolute inset-y-0 left-0 w-[2.5px] bg-amber" aria-hidden />
                    <div className="text-[12.5px] font-medium text-ink">
                      {NODE_BY_KEY[key].ord} · {NODE_BY_KEY[key].title}
                    </div>
                    <p className="mt-0.5 text-[11.5px] leading-relaxed text-ink-2">
                      Not connected to anything — it reads from{" "}
                      {needs.map((n) => NODE_BY_KEY[n].title).join(" and ")}, which{" "}
                      {needs.length === 1 ? "is" : "are"} not on the canvas.
                    </p>
                  </div>
                ))}
              </div>
            </section>
          )}

          {missingRecommended.length > 0 && (
            <section>
              <h3 className="text-[10.5px] font-semibold uppercase tracking-[0.09em] text-amber">
                Recommended
              </h3>
              <div className="mt-1.5 space-y-1.5">
                {missingRecommended.map((key) => (
                  <MissingRow key={key} nodeKey={key} tone="warn" />
                ))}
              </div>
            </section>
          )}
        </div>

        <footer className="flex items-center justify-between gap-3 border-t border-line px-5 py-3.5">
          <Button variant="ghost" onClick={onCancel}>
            Cancel
          </Button>
          <div className="flex gap-2">
            {!blocked && (
              <Button onClick={onRunAnyway}>Run without them</Button>
            )}
            <Button variant="primary" onClick={onAddAndRun}>
              {blocked ? "Add them and run" : "Add them and run"}
            </Button>
          </div>
        </footer>
      </div>
    </div>
  );
}

function MissingRow({ nodeKey, tone }: { nodeKey: NodeKey; tone: "bad" | "warn" }) {
  const def = NODE_BY_KEY[nodeKey];
  return (
    <div className="relative overflow-hidden rounded-[7px] border border-line px-2.5 py-2 pl-3">
      <span
        className={`absolute inset-y-0 left-0 w-[2.5px] ${tone === "bad" ? "bg-rust" : "bg-amber"}`}
        aria-hidden
      />
      <div className="flex items-baseline gap-2">
        <span className="font-mono text-[10.5px] font-semibold tabular-nums text-ink-3">
          {def.ord}
        </span>
        <span className="text-[12.5px] font-medium text-ink">{def.title}</span>
      </div>
      <p className="mt-0.5 text-[11.5px] leading-relaxed text-ink-2">
        {def.costOfSkipping ?? def.summary}
      </p>
    </div>
  );
}
