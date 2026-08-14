"use client";

/**
 * The canvas arrangement, remembered per workspace.
 *
 * Where a reviewer put a node, and which nodes they took off, are decisions about how
 * they want to read this verification — not scratch state. Losing them on navigation
 * meant a deleted node reappeared the moment you came back, and a graph you had laid
 * out to follow one customer reset to the default the next time you opened it.
 *
 * Kept in localStorage rather than on the server: it is a per-person view preference,
 * it must survive a reload with no round trip, and it holds nothing about the evidence
 * — only coordinates and a list of hidden node keys.
 */

import type { NodeKey } from "@/lib/graph";

export interface CanvasLayout {
  positions: Partial<Record<NodeKey, { x: number; y: number }>>;
  removed: NodeKey[];
}

const EMPTY: CanvasLayout = { positions: {}, removed: [] };

function keyFor(workspaceId: string): string {
  return `revenueproof.canvas.${workspaceId}`;
}

export function loadLayout(workspaceId: string): CanvasLayout {
  if (typeof window === "undefined") return EMPTY;
  try {
    const raw = window.localStorage.getItem(keyFor(workspaceId));
    if (!raw) return EMPTY;
    const parsed = JSON.parse(raw) as Partial<CanvasLayout>;
    return {
      positions: parsed.positions ?? {},
      removed: Array.isArray(parsed.removed) ? parsed.removed : [],
    };
  } catch {
    // A corrupted entry is not worth failing the page over; the default layout is
    // always correct, just not the one the reviewer arranged.
    return EMPTY;
  }
}

export function saveLayout(workspaceId: string, layout: CanvasLayout): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(keyFor(workspaceId), JSON.stringify(layout));
  } catch {
    /* Private browsing, or a full quota. The canvas still works; it just forgets. */
  }
}

export function clearLayout(workspaceId: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(keyFor(workspaceId));
  } catch {
    /* nothing to do */
  }
}
