/**
 * Types mirroring the backend's Pydantic schemas.
 *
 * Money is deliberately never a `number`. The backend sends minor units (exact
 * integers), a decimal string and a preformatted display string; the UI only ever
 * renders those. Doing arithmetic here would reintroduce the floating-point error
 * the whole backend is built to avoid.
 */

export interface Money {
  minor: number;
  currency: string;
  decimal: string;
  display: string;
}

export interface Workspace {
  id: string;
  company_name: string;
  legal_name: string | null;
  reporting_period_start: string;
  reporting_period_end: string;
  base_currency: string;
  claimed_revenue: Money;
  claimed_arr: Money;
  materiality_threshold_pct: number;
  accounting_method: string;
  active_policy_version: string;
  created_at: string;
}

export interface ConnectionStatus {
  source_system: string;
  is_active: boolean;
  is_synthetic: boolean;
  is_test_mode: boolean;
  last_sync_at: string | null;
  last_sync_status: string | null;
  last_sync_error: string | null;
  records_imported: number;
}

export interface RunStatus {
  id: string;
  status: string;
  current_stage: string | null;
  progress_pct: number;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  stage_stats: Record<string, unknown>;
}

export interface WorkspaceSummary {
  workspace: Workspace;
  evidence_counts: Record<string, number>;
  connections: ConnectionStatus[];
  /** Which providers this deployment can reach with its own credentials — capability,
   *  not what the last fetch happened to serve. */
  deployment_providers?: Record<string, boolean>;
  latest_run: RunStatus | null;
  open_review_items: number;
  quarantined_records: number;
}

/** One line in the live processing trace (§10.3). */
export interface TraceEvent {
  event_id: string;
  timestamp: string;
  kind:
    | "system"
    | "api_call"
    | "agent_step"
    | "tool_call"
    | "rule"
    | "persistence"
    | "test"
    | "error"
    | "result"
    | "progress";
  severity: "debug" | "info" | "success" | "warning" | "error";
  workspace_id: string;
  feature: number | null;
  message: string;
  data: Record<string, unknown>;
  run_id: string | null;
  duration_ms: number | null;
}

export interface AuthUser {
  id: string;
  email: string;
  full_name: string | null;
  is_platform_admin: boolean;
  workspace_count: number;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  user_id: string;
  email: string;
  full_name: string | null;
}

export interface HealthStatus {
  status: string;
  services: {
    postgres: { ok: boolean; version?: string; error?: string };
    redis: { ok: boolean; error?: string };
    neo4j: { ok: boolean; error?: string };
    llm: {
      ok: boolean;
      configured?: boolean;
      reason?: string;
      proposer?: string;
      critic?: string;
    };
  };
  providers: Record<string, boolean>;
  environment: string;
}

export interface AuditEntry {
  sequence: number;
  timestamp: string;
  actor: string;
  action: string;
  object_type: string;
  object_id: string;
  reason: string | null;
  before_state: Record<string, unknown> | null;
  after_state: Record<string, unknown> | null;
  event_hash: string;
}

export interface AuditResponse {
  integrity: { valid: boolean; checked: number; error: string | null };
  events: AuditEntry[];
}

// --- Feature 1: evidence ingestion ----------------------------------------

export interface IngestionStats {
  fetched: number;
  vaulted: number;
  duplicates: number;
  new_versions: number;
  normalized: number;
  quarantined: number;
  canonical_written: number;
  skipped_no_normalizer: number;
  errors: string[];
  by_type: Record<string, number>;
  is_synthetic: boolean;
}

export interface IngestionRun {
  run_id: string;
  sources: Record<string, IngestionStats>;
  total_canonical: number;
  error?: string;
}

export interface EvidenceRecord {
  id: string;
  source_system: string;
  record_type: string;
  source_id: string;
  /** SHA-256 of the canonical JSON — detects a semantic change in the source. */
  content_hash: string;
  /** SHA-256 of the original bytes, for file-backed evidence. */
  file_hash: string | null;
  version: number;
  retrieved_at: string;
  has_file: boolean;
  file_size_bytes: number | null;
}

export interface EvidenceResponse {
  counts: { record_type: string; source_system: string; count: number }[];
  records: EvidenceRecord[];
}

export interface QuarantineRecord {
  id: string;
  source_system: string;
  record_type: string;
  source_id: string | null;
  reason: string;
  detail: string;
  validation_errors: { field: string; message: string; type: string }[];
  created_at: string;
}

export interface QuarantineResponse {
  summary: {
    total: number;
    by_reason: Record<string, number>;
    by_source: Record<string, number>;
  };
  records: QuarantineRecord[];
}

// --- Feature 2: identity resolution ---------------------------------------

export interface ResolvedCustomer {
  id: string;
  canonical_name: string;
  normalized_name: string;
  known_aliases: string[];
  domains: string[];
  tax_identifiers: string[];
  email_addresses: string[];
  match_confidence: number | null;
  human_confirmed: boolean;
  related_party_status: string | null;
}

export interface MatchSignal {
  field: string;
  outcome: string;
  weight: number;
  detail: string;
}

export interface MatchProposal {
  id: string;
  left: { id: string; label: string; type: string };
  right: { id: string; label: string; type: string };
  method: string;
  score: number;
  decision: "ACCEPTED" | "REVIEW" | "REJECTED";
  signals: MatchSignal[];
  critic_note: string | null;
}

export interface ResolutionRun {
  records_considered: number;
  pairs_generated: number;
  accepted: number;
  review: number;
  rejected: number;
  clusters: number;
  critic_disputes: number;
  memory_applied: number;
  /** Merges refused because they would have combined known-different entities. */
  blocked_merges: { would_have_merged: string[]; reason: string }[];
  transitivity_conflicts: unknown[];
  review_items_created: number;
  evaluation: {
    precision?: number;
    recall?: number;
    auto_merge_permitted: boolean;
    labelled_pairs_evaluated: number;
    note?: string;
  };
}

// --- Feature 3: contract intelligence --------------------------------------

export interface ContractRow {
  id: string;
  document_name: string;
  stated_customer_name: string | null;
  start_date: string | null;
  end_date: string | null;
  billing_frequency: string;
  recurring_amount: Money;
  one_time_amount: Money;
  future_period_amount: Money;
  auto_renewal: boolean | null;
  termination_notice_days: number | null;
  is_scanned: boolean;
  ocr_applied: boolean;
  is_amendment: boolean;
  supersedes_contract_id: string | null;
  /** Share of citations that survived re-checking against the source page. */
  extraction_confidence: number | null;
  unknown_fields: string[];
  needs_human_review: boolean;
  review_reasons: string[];
}

export interface ContractCitation {
  field_name: string;
  field_value: string | null;
  page_number: number;
  quote: string;
  quote_hash: string;
  span: [number | null, number | null];
  bbox: number[] | null;
  verified: boolean;
  verification_note: string | null;
}

export interface ContractRun {
  processed: number;
  extracted: number;
  needs_review: number;
  failed: number;
  ocr_used: number;
  amendments_resolved: number;
  outcomes: {
    document_name: string;
    ocr_applied: boolean;
    citations: string;
    needs_review: boolean;
    review_reasons: string[];
    unknown_fields: string[];
    recurring_minor: number;
    one_time_minor: number;
    in_period_minor: number;
    error: string | null;
  }[];
}

// --- Feature 4: cash reconciliation ---------------------------------------

export interface InvoiceOutcome {
  invoice_id: string;
  invoice_number: string | null;
  customer: string | null;
  currency: string;
  total_minor: number;
  allocated_minor: number;
  outstanding_minor: number;
  refunded_minor: number;
  /** Cash applied, less anything later refunded or credited. */
  retained_minor: number;
  bank_confirmed_minor: number;
  fully_settled: boolean;
  bank_confirmed: boolean;
  payment_ids: string[];
  notes: string[];
}

export interface ReconciliationRun {
  solver_status: string;
  conservation_ok: boolean;
  conservation_error: string | null;
  invoices_considered: number;
  payments_considered: number;
  candidate_links: number;
  allocations_written: number;
  failed_payments: number;
  unsupported_receipts: number;
  invoices_unpaid: number;
  review_items_created: number;
  totals: Record<string, Money>;
  unapplied_cash: Money;
  outcomes: InvoiceOutcome[];
}

// --- Feature 5: revenue truth ---------------------------------------------

export type RevenueClassName =
  | "VERIFIED_RECURRING"
  | "VERIFIED_ONE_TIME"
  | "CONTRACTED_UNPAID"
  | "INVOICED_UNPAID"
  | "REFUNDED_OR_REVERSED"
  | "PAYMENT_WITHOUT_SUPPORT"
  | "UNSUPPORTED_CLAIM"
  | "HUMAN_REVIEW";

export interface RevenueTotals {
  currency: string;
  claimed_revenue: number;
  claimed_arr: number;
  cash_received: number;
  verified_recurring: number;
  verified_one_time: number;
  total_verified: number;
  contracted_unpaid: number;
  invoiced_unpaid: number;
  refunded_reversed: number;
  payment_without_support: number;
  unsupported_claim: number;
  human_review: number;
  supported_arr: number;
  unexplained_claim: number;
  arr_gap: number;
}

export interface WaterfallStep {
  label: string;
  amount_minor: number;
  kind: "start" | "deduction" | "addition" | "total";
  note?: string;
  money: Money;
}

export interface RevenueRun {
  policy_version: string;
  items_classified: number;
  by_class: Partial<Record<RevenueClassName, number>>;
  totals: RevenueTotals;
  material_items: number;
  items_awaiting_review: number;
  contracts_unread: number;
  double_count_conflicts: { evidence_id: string; items: string[]; reason: string }[];
  review_items_created: number;
  waterfall: WaterfallStep[];
  concentration: { customer: string; amount_minor: number; share_pct: number }[];
  money: Record<string, Money>;
  policy: Record<string, unknown> & { caveat: string };
}

export interface ClassifiedItem {
  id: string;
  description: string;
  classification: RevenueClassName;
  is_recurring: boolean;
  evidence_strength: "STRONG" | "MODERATE" | "LIMITED" | "DISPUTED";
  gross: Money;
  recognized: Money;
  rule_id: string;
  rule_explanation: string;
  evidence_ids: string[];
  missing_evidence: string[];
  calculation_detail: Record<string, unknown>;
  is_material: boolean;
  is_published: boolean;
  policy_version: string;
}

// --- Feature 6: anomaly detection ---------------------------------------

export type AnomalySeverityName = "high" | "medium" | "low" | "info";

export interface AnomalyFinding {
  id: string;
  rule_id: string;
  title: string;
  severity: AnomalySeverityName;
  explanation: string;
  required_check: string;
  observed_value: string | null;
  baseline_value: string | null;
  related_records: { type: string; id?: string; name?: string }[];
  graph_path: { source: string; target: string }[];
  caveats: string[];
  customer_entity_id: string | null;
  model_version: string | null;
  model_score: number | null;
  status: string;
  is_false_positive: boolean | null;
}

export interface AnomalyPrecision {
  total_findings: number;
  labelled: number;
  overall_precision: number | null;
  per_rule: Record<
    string,
    {
      total: number;
      labelled: number;
      confirmed: number;
      false_positive: number;
      precision: number | null;
    }
  >;
  ml_labelled: number;
  ml_precision: number | null;
  ml_enabled: boolean;
  ml_reason: string;
  min_labels_for_gate: number;
  precision_floor: number;
  note: string;
}

export interface AnomalyRun {
  run_id: string;
  findings_total: number;
  by_rule: Record<string, number>;
  by_severity: Partial<Record<AnomalySeverityName, number>>;
  ml: {
    enabled: boolean;
    reason: string;
    model_version: string | null;
    records_scored: number;
    validation: {
      method?: string;
      mean_flagged_rate?: number | null;
      rate_spread?: number | null;
      drift_suspected?: boolean;
      error?: string;
    };
  };
  graph: {
    gds_available: boolean;
    method: string;
    clusters: { members: { id: string; label: string; kind: string }[]; customer_count: number }[];
    cycles: { path: string[] }[];
  };
  concentration: {
    currency: string;
    total_verified_minor: number;
    customer_count: number;
    top_customer: string | null;
    top_share_pct: number;
    top_n_share_pct: number;
    top_n: number;
    hhi: number;
    hhi_caveat: string;
    per_customer: { customer: string; amount_minor: number; share_pct: number }[];
  };
  precision: AnomalyPrecision;
  narrative: {
    eligible: number;
    attempted: number;
    written: number;
    rejected: number;
    skipped: number;
    failed: number;
    reason: string;
  };
  anomalies_persisted: number;
  feedback_preserved: number;
  anomalies_retired: number;
  review_items_created: number;
  scanned: Record<string, number>;
  findings: (Omit<AnomalyFinding, "id" | "status" | "is_false_positive"> & {
    narrative: {
      summary: string;
      why_it_matters: string;
      what_would_resolve_it: string;
    } | null;
    narrative_status: string;
  })[];
}

// --- Feature 7: human review queue ----------------------------------------

export interface ReviewItemRow {
  id: string;
  category: string;
  raised_by: string;
  title: string;
  detail: string;
  severity: AnomalySeverityName;
  status: string;
  evidence_packet: Record<string, unknown>;
  anomaly_id: string | null;
  revenue_item_id: string | null;
  contract_id: string | null;
  resolution: string | null;
  resolution_reason: string | null;
  resolved_at: string | null;
  created_at: string | null;
  /** Every record this one decision covers. */
  member_ids: string[];
  member_count: number;
  also_affects: string[];
}

export interface ReviewQueue {
  summary: {
    open: number;
    open_decisions: number;
    in_progress: number;
    resolved: number;
    dismissed: number;
    by_category: Record<string, number>;
    by_severity: Partial<Record<AnomalySeverityName, number>>;
    oldest_open_days: number | null;
    total: number;
  };
  items: ReviewItemRow[];
  decisions: string[];
  can_resolve: boolean;
}

export interface CriticDecisionRow {
  id: string;
  revenue_item_id: string;
  description: string;
  classification: RevenueClassName;
  recognized_minor: number;
  is_published: boolean;
  verdict: "APPROVED" | "DISPUTED" | "MORE_EVIDENCE_REQUIRED";
  issue_codes: string[];
  reasoning: string;
  requested_evidence: string[];
  deterministic_findings: { code: string; detail: string }[];
  routed_to_feature: number | null;
  critic_model: string | null;
}

export interface CriticRun {
  run_id: string;
  items_reviewed: number;
  approved: number;
  disputed: number;
  more_evidence: number;
  published: number;
  unpublished: number;
  review_items_created: number;
  critic: {
    reviewed: number;
    by_verdict: Record<string, number>;
    by_issue: Record<string, number>;
    routed_to: Record<string, number>;
    model_calls: number;
    settled_deterministically: number;
  };
}

// --- Feature 8: living evidence graph and diligence room -------------------

export interface EvidenceNode {
  kind: "customer" | "contract" | "invoice" | "payment" | "bank" | "refund";
  id: string;
  label: string;
  [key: string]: unknown;
}

export interface EvidenceTrace {
  item_id: string;
  description: string;
  classification: string;
  is_published: boolean;
  recognized: Money;
  gross: Money;
  rule_id: string;
  rule_explanation: string;
  missing_evidence: string[];
  nodes: EvidenceNode[];
  edges: { source: string; target: string }[];
  critic: {
    verdict: string;
    issue_codes: string[];
    reasoning: string;
    routed_to_feature: number | null;
    settled_by: string;
  } | null;
  breaks: string[];
  complete: boolean;
}

export interface ReportVersionRow {
  version: number;
  published_at: string | null;
  currency: string;
  claimed_revenue: string;
  verified_recurring: string;
  verified_one_time: string;
  supported_arr: string;
  refunded_reversed: string;
  unsupported: string;
  items_awaiting_review: number;
  largest_customer_concentration_pct: number | null;
  hhi: number | null;
  changes_from_previous: {
    field: string;
    label: string;
    before: string;
    after: string;
    direction: "increased" | "decreased";
  }[];
  change_explanation: string | null;
  policy_version: string;
}

export interface DiligenceRoomData {
  position: {
    currency: string;
    claimed_revenue: number;
    claimed_arr: number;
    verified_recurring: number;
    verified_one_time: number;
    cash_received: number;
    contracted_unpaid: number;
    invoiced_unpaid: number;
    refunded_reversed: number;
    unsupported: number;
    supported_arr: number;
    items_awaiting_review: number;
    review_records: number;
    open_anomalies: number;
    items_published: number;
    items_total: number;
    largest_customer_concentration_pct: number | null;
    hhi: number | null;
    concentration_customers: number;
    concentration_basis: string;
    missing_evidence: { gap: string; items: number }[];
    policy_version: string;
  };
  history: ReportVersionRow[];
  why_the_gap: WhyTheGap;
  items: {
    id: string;
    description: string;
    classification: string;
    counts_as_verified: boolean;
    recognized: Money;
    gross: Money;
    is_published: boolean;
    rule_id: string | null;
    missing_evidence: string[];
    withheld_because: string | null;
    verdict: string | null;
  }[];
  published_count: number;
  withheld_count: number;
  caveat: string;
}

export interface ChangeImpact {
  checked_since: string;
  changes: {
    record_type: string;
    source_id: string;
    source_system: string;
    version: number;
    detected_at: string;
    customer_names: string[];
    affected_features: number[];
    affected_feature_names: string[];
    note: string;
  }[];
  features_to_rerun: number[];
  feature_names: string[];
  affected_customers: string[];
  affected_items: number;
  unchanged: boolean;
  summary: string;
  monitoring: {
    last_evidence_at: string | null;
    records_with_newer_versions: number;
    refunds_recorded: number;
    note: string;
  };
}

// --- Running the agents ----------------------------------------------------

export interface PipelineStage {
  key: string;
  feature: number;
  label: string;
  purpose: string;
  needs: string[];
  has_run: boolean;
}

export interface PipelineState {
  has_evidence: boolean;
  raw_records: number;
  invoices: number;
  payments: number;
  completed: Record<string, boolean>;
  stages: PipelineStage[];
}

export interface PipelineRun {
  run_id: string;
  ok: boolean;
  /** Why nothing ran — no evidence, or a stage whose prerequisite is missing. */
  blocked: string | null;
  /** What to do about it, in the reader's terms rather than the schema's. */
  remedy: string | null;
  seconds: number;
  stages_run: number;
  stages_failed: number;
  stages: {
    key: string;
    label: string;
    feature: number;
    status: "ran" | "skipped" | "failed" | "pending";
    detail: string;
    seconds: number;
  }[];
}

export interface ContractUploadResult {
  run_id: string;
  vaulted: number;
  accepted: { filename: string; size_bytes: number; outcome: string }[];
  rejected: { filename: string; reason: string }[];
}

/** Why the published figure sits below the claim, in causes a person can act on. */
export interface WhyTheGap {
  material: boolean;
  shortfall?: number;
  claim_may_be_wrong?: boolean;
  causes: {
    classification: string;
    count: number;
    amount: number;
    why: string;
  }[];
  actions: {
    summary: string;
    remedy: string;
    count: number;
    amount: number;
  }[];
}
