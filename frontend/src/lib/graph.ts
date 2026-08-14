/**
 * The verification graph: what the nine nodes are, what each needs, and what state
 * each is in right now.
 *
 * The dependency rules here are a *mirror* of the backend's, never the authority.
 * `features/pipeline.py` refuses a stage whose prerequisite has not run, and refuses
 * an empty workspace outright. This file exists so the canvas can explain a refusal
 * before the click rather than after it — a locked node that says what it is waiting
 * for is worth more than an enabled one that fails.
 */

export type NodeKey =
  | "evidence"
  | "identity"
  | "contracts"
  | "reconcile"
  | "revenue"
  | "anomalies"
  | "critic"
  | "review"
  | "publish";

export type NodeStatus =
  | "required"   // must be done before anything else can be
  | "ready"      // prerequisites met, not yet run
  | "running"
  | "complete"
  | "stale"      // ran, but something upstream has since re-run
  | "waiting"    // needs a person
  | "locked"     // prerequisites not met
  | "failed";

export interface NodeDef {
  key: NodeKey;
  /** Ordinal shown on the card. The chain really is a sequence, so it is numbered. */
  ord: string;
  title: string;
  /** One line, in the reviewer's terms, about what this node does. */
  summary: string;
  /** Why this node exists at all — shown in the inspector. */
  rationale: string;
  agent: string;
  needs: NodeKey[];
  /** The backend stage key, where one exists. `evidence` and `review` are not stages. */
  stage: string | null;
  group: "evidence" | "verification" | "controls" | "output";
  /** What travels along the edge out of this node. */
  emits: string;
  /**
   * `required` — the chain cannot produce a figure without it, and the backend will
   * refuse the stages beneath it. `recommended` — the run completes without it, but
   * the result is weaker in a way worth naming before someone accepts it.
   */
  importance: "required" | "recommended";
  /** What is lost by running without it. Shown when a reviewer skips it. */
  costOfSkipping?: string;
}

export const NODES: NodeDef[] = [
  {
    key: "evidence",
    ord: "01",
    title: "Load Evidence",
    summary: "Connect the records that support the claim.",
    rationale:
      "The required starting point. Nothing can be verified until evidence is loaded, and every downstream node reads from what lands here.",
    agent: "Evidence Collector",
    needs: [],
    stage: null,
    group: "evidence",
    emits: "Evidence packages",
    importance: "required",
  },
  {
    key: "identity",
    ord: "02",
    title: "Resolve Identity",
    summary: "Decide which records are the same customer.",
    rationale:
      "One customer spelled four ways across five systems understates concentration; two different companies merged overstates it. Both directions matter, so uncertain matches go to a person.",
    agent: "Identity Resolver",
    needs: ["evidence"],
    stage: "identity",
    group: "verification",
    emits: "Resolved entities",
    importance: "required",
  },
  {
    key: "contracts",
    ord: "03",
    title: "Read Contracts",
    summary: "Separate recurring value from one-time fees.",
    rationale:
      "An invoice description is not a contract. Every extracted amount carries a page citation that is re-verified against the document; a value whose citation fails is discarded rather than used.",
    agent: "Contract Intelligence",
    needs: ["evidence"],
    stage: "contracts",
    group: "verification",
    emits: "Extracted terms",
    importance: "recommended",
    costOfSkipping:
      "Nothing can be classified as recurring, so supported ARR reads as zero and a one-time fee sold as a subscription goes uncaught.",
  },
  {
    key: "reconcile",
    ord: "04",
    title: "Reconcile Cash",
    summary: "Match invoices to payments to bank receipts.",
    rationale:
      "Answers the question the product exists for: did the money arrive, and did the company keep it? Solved as a constraint problem so double-counting is structurally impossible.",
    agent: "Cash Reconciler",
    needs: ["identity"],
    stage: "reconcile",
    group: "verification",
    emits: "Reconciled cash",
    importance: "required",
  },
  {
    key: "revenue",
    ord: "05",
    title: "Verify Revenue",
    summary: "Classify every amount against the claim.",
    rationale:
      "Sorts each amount into one of eight states and sets the total beside what was claimed. Refunds are checked before any verification rule, because a refunded item has complete-looking evidence.",
    agent: "Revenue Verifier",
    needs: ["identity", "reconcile"],
    stage: "revenue",
    group: "verification",
    emits: "Verified revenue",
    importance: "required",
  },
  {
    key: "anomalies",
    ord: "06",
    title: "Detect Anomalies",
    summary: "Find what is odd, and say why.",
    rationale:
      "Rules, an explainable model and a graph search run independently and are then joined. Findings are indicators requiring review — never accusations.",
    agent: "Anomaly Detector",
    needs: ["revenue"],
    stage: "anomalies",
    group: "controls",
    emits: "Indicators",
    importance: "recommended",
    costOfSkipping:
      "Duplicate payments, circular flows and related parties are not looked for, and the critic loses the indicators it uses to withhold a figure.",
  },
  {
    key: "critic",
    ord: "07",
    title: "Independent Critic",
    summary: "Argue against every classification.",
    rationale:
      "A second model from a different family re-reads the original evidence and tries to knock each figure down. It can only ever weaken a claim, and it cannot withhold one that passed every arithmetic check.",
    agent: "Adversarial Critic",
    needs: ["revenue"],
    stage: "critic",
    group: "controls",
    emits: "Verdicts",
    importance: "required",
  },
  {
    key: "review",
    ord: "08",
    title: "Human Review",
    summary: "Settle what the agents could not.",
    rationale:
      "Equivalent questions are collapsed into one decision covering many records. A decision cannot be recorded without a written reason.",
    agent: "Review Queue",
    needs: ["critic"],
    stage: null,
    group: "controls",
    emits: "Review outcomes",
    importance: "recommended",
    costOfSkipping:
      "Nothing is settled by a person, so anything the agents could not agree on stays withheld.",
  },
  {
    key: "publish",
    ord: "09",
    title: "Publish Diligence Report",
    summary: "Freeze the position so it can be compared later.",
    rationale:
      "Publishing snapshots every headline figure. The next run reports what moved, in which direction and by how much — computed in code, never narrated by a model.",
    agent: "Diligence Room",
    needs: ["critic"],
    stage: "publish",
    group: "output",
    emits: "Published version",
    importance: "recommended",
    costOfSkipping:
      "The position is not frozen, so there is nothing to compare the next run against.",
  },
];

export const NODE_BY_KEY: Record<NodeKey, NodeDef> = Object.fromEntries(
  NODES.map((n) => [n.key, n]),
) as Record<NodeKey, NodeDef>;

/** Left-to-right columns. The chain reads as a sequence because it is one. */
export const LAYOUT: Record<NodeKey, { x: number; y: number }> = {
  evidence: { x: 0, y: 150 },
  identity: { x: 330, y: 40 },
  contracts: { x: 330, y: 265 },
  reconcile: { x: 660, y: 40 },
  revenue: { x: 990, y: 150 },
  anomalies: { x: 1320, y: 40 },
  critic: { x: 1320, y: 265 },
  review: { x: 1650, y: 265 },
  publish: { x: 1980, y: 150 },
};

export const GROUP_LABEL: Record<NodeDef["group"], string> = {
  evidence: "Evidence",
  verification: "Verification",
  controls: "Controls",
  output: "Output",
};

/**
 * Why a node cannot be added or run yet, phrased as the thing to go and do.
 * Returns null when it is available.
 */
export function blockedReason(key: NodeKey, present: Set<NodeKey>): string | null {
  const def = NODE_BY_KEY[key];
  const missing = def.needs.filter((need) => !present.has(need));
  if (missing.length === 0) return null;
  const names = missing.map((k) => NODE_BY_KEY[k].title);
  return names.length === 1
    ? `Add ${names[0]} first`
    : `Add ${names.slice(0, -1).join(", ")} and ${names[names.length - 1]} first`;
}

/** Every node that must have *completed* before this one may run. */
export function unmetPrerequisites(
  key: NodeKey,
  completed: Set<NodeKey>,
): NodeKey[] {
  return NODE_BY_KEY[key].needs.filter((need) => !completed.has(need));
}

/** Everything downstream of a node, transitively — what a rerun invalidates. */
export function downstreamOf(key: NodeKey): NodeKey[] {
  const out = new Set<NodeKey>();
  const walk = (from: NodeKey) => {
    for (const node of NODES) {
      if (node.needs.includes(from) && !out.has(node.key)) {
        out.add(node.key);
        walk(node.key);
      }
    }
  };
  walk(key);
  return [...out];
}

/** Colour and label for one state. The single place status styling is decided. */
export const STATUS_STYLE: Record<
  NodeStatus,
  { label: string; text: string; bg: string; dot: string; border: string; rail: string }
> = {
  required: {
    label: "Required",
    text: "text-amber",
    bg: "bg-amber-soft",
    dot: "bg-amber",
    border: "border-amber/40",
    rail: "bg-amber",
  },
  ready: {
    label: "Ready",
    text: "text-ink-2",
    bg: "bg-slate-soft",
    dot: "bg-slate",
    border: "border-line",
    rail: "bg-slate",
  },
  running: {
    label: "Running",
    text: "text-cobalt",
    bg: "bg-cobalt-soft",
    dot: "bg-cobalt",
    border: "border-cobalt",
    rail: "bg-cobalt",
  },
  complete: {
    label: "Complete",
    text: "text-emerald",
    bg: "bg-emerald-soft",
    dot: "bg-emerald",
    border: "border-emerald",
    rail: "bg-emerald",
  },
  stale: {
    label: "Out of date",
    text: "text-amber",
    bg: "bg-amber-soft",
    dot: "bg-amber",
    border: "border-amber/50",
    rail: "bg-amber",
  },
  waiting: {
    label: "Waiting on you",
    text: "text-amber",
    bg: "bg-amber-soft",
    dot: "bg-amber",
    border: "border-amber/50",
    rail: "bg-amber",
  },
  locked: {
    label: "Locked",
    text: "text-ink-3",
    bg: "bg-slate-soft",
    dot: "bg-slate",
    border: "border-line",
    rail: "bg-slate/60",
  },
  failed: {
    label: "Failed",
    text: "text-rust",
    bg: "bg-rust-soft",
    dot: "bg-rust",
    border: "border-rust/50",
    rail: "bg-rust",
  },
};

/**
 * Which export each node hands over. These are the backend's own artifact keys
 * (`features/review/exports.py`), so a node's download button fetches a real file
 * built from real rows rather than anything assembled in the browser.
 */
export const DOWNLOAD_KEY: Record<NodeKey, string> = {
  evidence: "evidence",
  identity: "customers",
  contracts: "contracts",
  reconcile: "reconciliation",
  revenue: "revenue-items",
  anomalies: "anomalies",
  critic: "review-queue",
  review: "review-queue",
  publish: "report",
};

/** The backend feature number each node's events are stamped with. */
export const NODE_FEATURE: Record<NodeKey, number> = {
  evidence: 1,
  identity: 2,
  contracts: 3,
  reconcile: 4,
  revenue: 5,
  anomalies: 6,
  critic: 7,
  review: 7,
  publish: 8,
};
