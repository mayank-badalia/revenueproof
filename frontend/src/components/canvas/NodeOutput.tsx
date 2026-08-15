"use client";

/**
 * What each node actually produced, read from the endpoint that owns it.
 *
 * The first version of this showed two summary rows per node and a download button.
 * That is a status display, not a diligence tool: the whole product is the *detail* —
 * which customers were merged and on what evidence, which citation failed to verify,
 * which invoice is outstanding by how much, what the critic objected to and why. A
 * node that says "24 customers" and nothing else has hidden the only part worth
 * reading.
 *
 * So every node renders its real rows here, and every table can be taken away as a
 * file. Nothing is computed in this file — each figure is displayed exactly as the
 * backend formatted it.
 */

import { useCallback, useEffect, useState } from "react";

import { api } from "@/lib/api";
import { fromMinor } from "@/lib/format";
import { DOWNLOAD_KEY, type NodeKey } from "@/lib/graph";
import { Banner, Button, DownloadIcon, Eyebrow, Spinner } from "@/components/ui/primitives";
import { ReviewDecisions } from "./ReviewDecisions";
import type {
  AnomalyFinding,
  ClassifiedItem,
  ContractRow,
  CriticDecisionRow,
  InvoiceOutcome,
  MatchProposal,
  EvidenceTrace,
  ResolvedCustomer,
  WhyTheGap,
  WorkspaceSummary,
} from "@/lib/types";

/* --- shared bits ---------------------------------------------------------- */

function Stat({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div className="rounded-[7px] border border-line px-2.5 py-1.5">
      <div className="text-[10.5px] text-ink-3">{label}</div>
      <div className={`mt-0.5 font-mono tnum text-[13px] font-medium ${tone ?? "text-ink"}`}>
        {value}
      </div>
    </div>
  );
}

function Stats({ children }: { children: React.ReactNode }) {
  return <div className="mb-3 grid grid-cols-2 gap-1.5">{children}</div>;
}

function Item({
  title,
  subtitle,
  right,
  tone,
  children,
}: {
  title: string;
  subtitle?: string;
  right?: React.ReactNode;
  tone?: "ok" | "warn" | "bad";
  children?: React.ReactNode;
}) {
  const rail =
    tone === "ok" ? "bg-emerald" : tone === "warn" ? "bg-amber" : tone === "bad" ? "bg-rust" : "bg-line";
  return (
    <div className="relative overflow-hidden rounded-[7px] border border-line px-2.5 py-2 pl-3">
      <span className={`absolute inset-y-0 left-0 w-[2.5px] ${rail}`} aria-hidden />
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="text-[12px] font-medium leading-snug text-ink">{title}</div>
          {subtitle && <div className="mt-0.5 text-[11px] text-ink-3">{subtitle}</div>}
        </div>
        {right && <div className="shrink-0 font-mono tnum text-[11.5px] text-ink-2">{right}</div>}
      </div>
      {children && <div className="mt-1.5">{children}</div>}
    </div>
  );
}

function Chips({ items, label }: { items: string[]; label?: string }) {
  if (!items.length) return null;
  return (
    <div className="mt-1 flex flex-wrap gap-1">
      {label && <span className="text-[10.5px] text-ink-3">{label}</span>}
      {items.slice(0, 8).map((value) => (
        <span
          key={value}
          className="rounded-[4px] bg-slate-soft px-1.5 py-[1px] font-mono text-[10px] text-ink-2"
        >
          {value}
        </span>
      ))}
      {items.length > 8 && (
        <span className="text-[10.5px] text-ink-3">+{items.length - 8} more</span>
      )}
    </div>
  );
}

function Empty({ what }: { what: string }) {
  return (
    <div className="rounded-[8px] border border-dashed border-line px-4 py-6 text-center">
      <p className="text-[12px] text-ink-2">{what}</p>
    </div>
  );
}

const SEVERITY_TONE = { high: "bad", medium: "warn", low: "ok", info: "ok" } as const;

/* --- the panel ------------------------------------------------------------ */

export function NodeOutput({
  workspaceId,
  nodeKey,
  currency,
  hasRun,
  onResolved,
}: {
  workspaceId: string;
  nodeKey: NodeKey;
  currency: string;
  hasRun: boolean;
  onResolved: () => void;
}) {
  const [data, setData] = useState<unknown>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const fetchers: Partial<Record<NodeKey, () => Promise<unknown>>> = {
        evidence: () => api.workspaceSummary(workspaceId),
        identity: async () => ({
          customers: await api.listResolvedCustomers(workspaceId),
          matches: await api.listMatches(workspaceId, "review"),
        }),
        contracts: () => api.listContracts(workspaceId),
        reconcile: () => api.reconciliation(workspaceId),
        revenue: async () => ({
          summary: await api.revenueSummary(workspaceId),
          items: await api.listRevenueItems(workspaceId),
        }),
        anomalies: () => api.listAnomalies(workspaceId),
        critic: () => api.listCriticDecisions(workspaceId),
        publish: () => api.diligenceRoom(workspaceId),
      };
      const fetcher = fetchers[nodeKey];
      setData(fetcher ? await fetcher() : null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not read this node's output");
    } finally {
      setLoading(false);
    }
  }, [workspaceId, nodeKey]);

  useEffect(() => {
    if (nodeKey !== "review" && hasRun) void load();
  }, [load, nodeKey, hasRun]);

  if (nodeKey === "review") {
    return <ReviewDecisions workspaceId={workspaceId} onResolved={onResolved} />;
  }

  if (!hasRun) {
    return <Empty what="Nothing yet. Run this node to produce something to inspect." />;
  }

  return (
    <div>
      <div className="mb-2.5 flex items-center justify-between">
        <Eyebrow>Output</Eyebrow>
        <Button
          size="sm"
          onClick={() => void api.downloadArtifact(workspaceId, DOWNLOAD_KEY[nodeKey])}
          icon={<DownloadIcon size={12} />}
        >
          Download
        </Button>
      </div>

      {loading && <Spinner className="text-ink-3" />}
      {error && <Banner tone="error">{error}</Banner>}

      {!loading && !error && data !== null && (
        <div className="space-y-1.5">
          {nodeKey === "evidence" && <EvidenceOut data={data as WorkspaceSummary} />}
          {nodeKey === "identity" && (
            <IdentityOut
              data={data as IdentityData}
              workspaceId={workspaceId}
              onChanged={() => void load()}
            />
          )}
          {nodeKey === "contracts" && <ContractsOut data={data as { contracts: ContractRow[] }} />}
          {nodeKey === "reconcile" && <ReconcileOut data={data as ReconcileData} currency={currency} />}
          {nodeKey === "revenue" && <RevenueOut data={data as RevenueData} currency={currency} />}
          {nodeKey === "anomalies" && (
            <AnomaliesOut
              data={data as { anomalies: AnomalyFinding[] }}
              workspaceId={workspaceId}
              onChanged={() => void load()}
            />
          )}
          {nodeKey === "critic" && <CriticOut data={data as { decisions: CriticDecisionRow[] }} currency={currency} />}
          {nodeKey === "publish" && (
            <PublishOut
              data={data as PublishData}
              currency={currency}
              workspaceId={workspaceId}
            />
          )}
        </div>
      )}
    </div>
  );
}

/* --- per node ------------------------------------------------------------- */

function EvidenceOut({ data }: { data: WorkspaceSummary }) {
  const counts = Object.entries(data.evidence_counts).filter(([, v]) => v > 0);
  return (
    <>
      <Stats>
        {counts.map(([key, value]) => (
          <Stat key={key} label={key.replace(/_/g, " ")} value={String(value)} />
        ))}
      </Stats>
      <Eyebrow>Sources</Eyebrow>
      <div className="mt-1.5 space-y-1.5">
        {data.connections.map((c) => (
          <Item
            key={c.source_system}
            title={String(c.source_system).replace(/_/g, " ")}
            subtitle={
              c.last_sync_error
                ? c.last_sync_error
                : `${c.is_synthetic ? "demonstration data" : "live account"} · ${c.last_sync_status ?? "not synced"}`
            }
            right={`${c.records_imported ?? 0}`}
            tone={c.last_sync_error ? "bad" : c.last_sync_status === "ok" ? "ok" : "warn"}
          />
        ))}
      </div>
      {data.quarantined_records > 0 && (
        <div className="mt-2">
          <Banner tone="warn">
            {data.quarantined_records} record(s) could not be read and were quarantined
            rather than dropped.
          </Banner>
        </div>
      )}
    </>
  );
}

interface IdentityData {
  customers: { customers: ResolvedCustomer[] };
  matches: { proposals: MatchProposal[] };
}

function IdentityOut({
  data,
  workspaceId,
  onChanged,
}: {
  data: IdentityData;
  workspaceId: string;
  onChanged: () => void;
}) {
  const customers = data.customers.customers ?? [];
  const proposals = data.matches?.proposals ?? [];
  const related = customers.filter((c) => c.related_party_status);
  return (
    <>
      <Stats>
        <Stat label="Customers resolved" value={String(customers.length)} />
        <Stat
          label="Awaiting a person"
          value={String(proposals.length)}
          tone={proposals.length ? "text-amber" : undefined}
        />
        <Stat label="Related parties" value={String(related.length)} />
        <Stat
          label="Human confirmed"
          value={String(customers.filter((c) => c.human_confirmed).length)}
        />
      </Stats>
      {proposals.length > 0 && (
        <div className="mb-3">
          <Eyebrow>Proposed merges awaiting a person</Eyebrow>
          <div className="mt-1.5 space-y-1.5">
            {proposals.map((m) => (
              <MatchDecision
                key={m.id}
                workspaceId={workspaceId}
                match={m}
                onDecided={onChanged}
              />
            ))}
          </div>
        </div>
      )}

      <Eyebrow>Resolved customers</Eyebrow>
      <div className="mt-1.5 space-y-1.5">
        {customers.length === 0 && <Empty what="No customers resolved." />}
        {customers.map((c) => (
          <Item
            key={c.id}
            title={c.canonical_name}
            subtitle={c.related_party_status ? `Related party — ${c.related_party_status}` : undefined}
            right={c.match_confidence !== null ? `${Math.round(c.match_confidence * 100)}%` : undefined}
            tone={c.related_party_status ? "warn" : c.human_confirmed ? "ok" : undefined}
          >
            <Chips items={c.known_aliases} label="also" />
            <Chips items={[...c.domains, ...c.tax_identifiers]} />
          </Item>
        ))}
      </div>
    </>
  );
}

function ContractsOut({ data }: { data: { contracts: ContractRow[] } }) {
  const rows = data.contracts ?? [];
  const read = rows.filter((c) => c.recurring_amount.minor > 0 || c.one_time_amount.minor > 0);
  return (
    <>
      <Stats>
        <Stat label="Contracts" value={String(rows.length)} />
        <Stat label="Terms extracted" value={String(read.length)} />
        <Stat label="Needed OCR" value={String(rows.filter((c) => c.ocr_applied).length)} />
        <Stat label="Amendments" value={String(rows.filter((c) => c.is_amendment).length)} />
      </Stats>
      <Eyebrow>Contracts</Eyebrow>
      <div className="mt-1.5 space-y-1.5">
        {rows.map((c) => {
          const unread = c.recurring_amount.minor === 0 && c.one_time_amount.minor === 0;
          return (
            <Item
              key={c.id}
              title={c.document_name}
              subtitle={c.stated_customer_name ?? "party not stated in the document"}
              tone={unread ? "warn" : "ok"}
            >
              <div className="mt-1 grid grid-cols-2 gap-1">
                <span className="font-mono tnum text-[11px] text-ink-2">
                  <span className="text-ink-3">recurring</span>{" "}
                  {unread ? "not read" : c.recurring_amount.display}
                </span>
                <span className="font-mono tnum text-[11px] text-ink-2">
                  <span className="text-ink-3">one-time</span>{" "}
                  {unread ? "not read" : c.one_time_amount.display}
                </span>
              </div>
              {c.start_date && (
                <div className="mt-0.5 font-mono text-[10.5px] text-ink-3">
                  {c.start_date} → {c.end_date ?? "open"} · {c.billing_frequency}
                  {c.ocr_applied ? " · OCR" : ""}
                </div>
              )}
            </Item>
          );
        })}
      </div>
    </>
  );
}

interface ReconcileData {
  solver_status: string;
  conservation_ok: boolean;
  allocations_written: number;
  invoices_unpaid: number;
  unsupported_receipts: number;
  failed_payments: number;
  totals: Record<string, { display: string }>;
  outcomes: InvoiceOutcome[];
}

function ReconcileOut({ data, currency }: { data: ReconcileData; currency: string }) {
  const outcomes = data.outcomes ?? [];
  return (
    <>
      <Stats>
        <Stat
          label="Solver"
          value={data.solver_status ?? "—"}
          tone={data.solver_status === "OPTIMAL" ? "text-emerald" : "text-amber"}
        />
        <Stat
          label="Conservation"
          value={data.conservation_ok ? "verified" : "failed"}
          tone={data.conservation_ok ? "text-emerald" : "text-rust"}
        />
        <Stat label="Allocations" value={String(data.allocations_written)} />
        <Stat
          label="Invoices unpaid"
          value={String(data.invoices_unpaid)}
          tone={data.invoices_unpaid ? "text-amber" : undefined}
        />
      </Stats>
      {Object.keys(data.totals ?? {}).length > 0 && (
        <div className="mb-3">
          <Eyebrow>Totals</Eyebrow>
          <div className="mt-1.5 space-y-1">
            {Object.entries(data.totals).map(([key, money]) => (
              <div key={key} className="flex justify-between border-b border-line-2 py-1 last:border-0">
                <span className="text-[12px] text-ink-2">{key.replace(/_/g, " ")}</span>
                <span className="font-mono tnum text-[12px] text-ink">{money.display}</span>
              </div>
            ))}
          </div>
        </div>
      )}
      <Eyebrow>Invoice settlement</Eyebrow>
      <div className="mt-1.5 space-y-1.5">
        {outcomes.length === 0 && <Empty what="No invoices were reconciled." />}
        {outcomes.map((o) => (
          <Item
            key={o.invoice_id}
            title={o.invoice_number ?? o.invoice_id.slice(0, 8)}
            subtitle={o.customer ?? "customer not resolved"}
            right={fromMinor(o.total_minor, currency)}
            tone={o.bank_confirmed ? "ok" : o.fully_settled ? "warn" : "bad"}
          >
            <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 font-mono tnum text-[10.5px] text-ink-2">
              <span>
                <span className="text-ink-3">applied</span> {fromMinor(o.allocated_minor, currency)}
              </span>
              {o.outstanding_minor > 0 && (
                <span className="text-amber">
                  <span className="text-ink-3">outstanding</span>{" "}
                  {fromMinor(o.outstanding_minor, currency)}
                </span>
              )}
              {o.refunded_minor > 0 && (
                <span className="text-rust">
                  <span className="text-ink-3">refunded</span>{" "}
                  {fromMinor(o.refunded_minor, currency)}
                </span>
              )}
              <span className={o.bank_confirmed ? "text-emerald" : "text-ink-3"}>
                {o.bank_confirmed ? "bank confirmed" : "no bank line"}
              </span>
            </div>
            {o.notes?.length > 0 && (
              <div className="mt-1 text-[10.5px] leading-snug text-ink-3">{o.notes.join(" · ")}</div>
            )}
          </Item>
        ))}
      </div>
    </>
  );
}

interface RevenueData {
  summary: {
    totals: Record<string, number> & { currency: string };
    waterfall: { label: string; amount_minor: number; kind: string; note?: string }[];
    by_class: Record<string, number>;
    concentration: { customer: string; amount_minor: number; share_pct: number }[];
  };
  items: { items: ClassifiedItem[] };
}

function RevenueOut({ data, currency }: { data: RevenueData; currency: string }) {
  const t = data.summary.totals;
  const items = data.items?.items ?? [];
  return (
    <>
      <Stats>
        <Stat label="Claimed" value={fromMinor(t.claimed_revenue, currency)} />
        <Stat
          label="Evidence-supported"
          value={fromMinor(t.total_verified, currency)}
          tone="text-emerald"
        />
        <Stat label="Invoiced, unpaid" value={fromMinor(t.invoiced_unpaid, currency)} />
        <Stat label="Refunded" value={fromMinor(t.refunded_reversed, currency)} tone="text-rust" />
      </Stats>

      {data.summary.waterfall?.length > 0 && (
        <div className="mb-3">
          <Eyebrow>How the claim resolves</Eyebrow>
          <div className="mt-1.5 space-y-1">
            {data.summary.waterfall.map((step) => (
              <div
                key={step.label}
                className={`flex justify-between gap-3 border-b border-line-2 py-1 last:border-0 ${
                  step.kind === "total" ? "font-medium text-ink" : "text-ink-2"
                }`}
              >
                <span className="text-[12px]">{step.label}</span>
                <span className="shrink-0 font-mono tnum text-[12px]">
                  {fromMinor(step.amount_minor, currency)}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {data.summary.concentration?.length > 0 && (
        <div className="mb-3">
          <Eyebrow>Customer concentration</Eyebrow>
          <div className="mt-1.5 space-y-1">
            {data.summary.concentration.slice(0, 5).map((c) => (
              <div key={c.customer} className="flex items-center gap-2">
                <span className="min-w-0 flex-1 truncate text-[11.5px] text-ink-2">{c.customer}</span>
                <span className="font-mono tnum text-[11.5px] text-ink">{c.share_pct}%</span>
              </div>
            ))}
          </div>
        </div>
      )}

      <Eyebrow>Classified items ({items.length})</Eyebrow>
      <div className="mt-1.5 space-y-1.5">
        {items.slice(0, 40).map((item) => (
          <Item
            key={item.id}
            title={item.description}
            subtitle={`${String(item.classification).replace(/_/g, " ").toLowerCase()} · ${item.rule_id}`}
            right={item.recognized.display}
            tone={
              item.is_published
                ? "ok"
                : String(item.classification).startsWith("VERIFIED")
                  ? "warn"
                  : undefined
            }
          >
            {item.missing_evidence?.length > 0 && (
              <div className="mt-1 text-[10.5px] text-amber">
                missing: {item.missing_evidence.join(", ")}
              </div>
            )}
          </Item>
        ))}
        {items.length > 40 && (
          <p className="pt-1 text-[11px] text-ink-3">
            Showing 40 of {items.length}. Download for all.
          </p>
        )}
      </div>
    </>
  );
}

function AnomaliesOut({
  data,
  workspaceId,
  onChanged,
}: {
  data: { anomalies: AnomalyFinding[] };
  workspaceId: string;
  onChanged: () => void;
}) {
  const rows = data.anomalies ?? [];
  const bySeverity = (s: string) => rows.filter((a) => a.severity === s).length;
  return (
    <>
      <Stats>
        <Stat label="Indicators" value={String(rows.length)} />
        <Stat label="High severity" value={String(bySeverity("high"))} tone="text-rust" />
        <Stat label="Medium" value={String(bySeverity("medium"))} tone="text-amber" />
        <Stat label="Rules fired" value={String(new Set(rows.map((a) => a.rule_id)).size)} />
      </Stats>
      <Eyebrow>Indicators requiring review</Eyebrow>
      <div className="mt-1.5 space-y-1.5">
        {rows.length === 0 && <Empty what="No indicators were raised." />}
        {rows.map((a) => (
          <Item
            key={a.id}
            title={a.title}
            subtitle={`${a.rule_id} · ${a.severity}`}
            tone={SEVERITY_TONE[a.severity as keyof typeof SEVERITY_TONE] ?? "warn"}
          >
            <p className="mt-1 text-[11px] leading-relaxed text-ink-2">{a.explanation}</p>
            {(a.observed_value || a.baseline_value) && (
              <div className="mt-1 font-mono text-[10.5px] text-ink-3">
                observed {a.observed_value ?? "—"} · baseline {a.baseline_value ?? "not recorded"}
              </div>
            )}
            <p className="mt-1 text-[10.5px] text-cobalt">What to check: {a.required_check}</p>
            {a.caveats?.length > 0 && (
              <ul className="mt-1 list-disc pl-4 text-[10.5px] text-ink-3">
                {a.caveats.map((c) => (
                  <li key={c}>{c}</li>
                ))}
              </ul>
            )}
            <AnomalyVerdict
              workspaceId={workspaceId}
              anomaly={a}
              onChanged={onChanged}
            />
          </Item>
        ))}
      </div>
    </>
  );
}

function CriticOut({
  data,
  currency,
}: {
  data: { decisions: CriticDecisionRow[] };
  currency: string;
}) {
  const rows = data.decisions ?? [];
  const count = (v: string) => rows.filter((d) => d.verdict === v).length;
  return (
    <>
      <Stats>
        <Stat label="Reviewed" value={String(rows.length)} />
        <Stat label="Approved" value={String(count("APPROVED"))} tone="text-emerald" />
        <Stat label="Disputed" value={String(count("DISPUTED"))} tone="text-rust" />
        <Stat label="Published" value={String(rows.filter((d) => d.is_published).length)} />
      </Stats>
      <Eyebrow>Verdicts</Eyebrow>
      <div className="mt-1.5 space-y-1.5">
        {rows.map((d) => (
          <Item
            key={d.id}
            title={d.description}
            subtitle={`${d.verdict.replace(/_/g, " ").toLowerCase()}${d.critic_model ? ` · ${d.critic_model}` : ""}`}
            right={fromMinor(d.recognized_minor, currency)}
            tone={d.verdict === "APPROVED" ? "ok" : d.verdict === "DISPUTED" ? "bad" : "warn"}
          >
            {d.reasoning && (
              <p className="mt-1 text-[11px] leading-relaxed text-ink-2">{d.reasoning}</p>
            )}
            {d.deterministic_findings?.length > 0 && (
              <div className="mt-1 space-y-0.5">
                {d.deterministic_findings.map((f) => (
                  <div key={f.code} className="text-[10.5px] text-ink-3">
                    <span className="font-mono text-ink-2">{f.code}</span> — {f.detail}
                  </div>
                ))}
              </div>
            )}
            <div className="mt-1 text-[10.5px]">
              {d.is_published ? (
                <span className="text-emerald">published</span>
              ) : (
                <span className="text-amber">withheld</span>
              )}
            </div>
          </Item>
        ))}
      </div>
    </>
  );
}

interface PublishData {
  position: Record<string, number | string | null>;
  why_the_gap?: WhyTheGap;
  items?: { id: string; description: string; amount: string; is_published: boolean; withheld_because: string | null }[];
}

function PublishOut({
  data,
  currency,
  workspaceId,
}: {
  data: PublishData;
  currency: string;
  workspaceId: string;
}) {
  const p = data.position;
  const num = (k: string) => (typeof p[k] === "number" ? (p[k] as number) : 0);
  const withheld = (data.items ?? []).filter((i) => !i.is_published);
  const published = (data.items ?? []).filter((i) => i.is_published);
  return (
    <>
      <Stats>
        <Stat label="Claimed" value={fromMinor(num("claimed_revenue"), currency)} />
        <Stat
          label="Proven and published"
          value={fromMinor(num("cash_received"), currency)}
          tone="text-emerald"
        />
        <Stat label="Items published" value={String(num("items_published"))} />
        <Stat
          label="Awaiting review"
          value={String(num("items_awaiting_review"))}
          tone={num("items_awaiting_review") ? "text-amber" : undefined}
        />
      </Stats>

      {data.why_the_gap?.material && (
        <div className="mb-3">
          <Eyebrow>Why the gap</Eyebrow>
          <p className="mt-1 text-[11.5px] leading-relaxed text-ink-2">
            {fromMinor(data.why_the_gap.shortfall, currency)} of the claim is not supported by
            published evidence.
            {data.why_the_gap.claim_may_be_wrong
              ? " Less than half the claim is proven, so the claim itself is one of the explanations."
              : ""}
          </p>
          <div className="mt-1.5 space-y-1.5">
            {data.why_the_gap.causes.map((cause) => (
              <Item
                key={cause.classification}
                title={fromMinor(cause.amount, currency)}
                subtitle={`${cause.count} item(s) · ${cause.classification.replace(/_/g, " ").toLowerCase()}`}
                tone="warn"
              >
                <p className="mt-0.5 text-[11px] leading-relaxed text-ink-2">{cause.why}</p>
              </Item>
            ))}
          </div>

          {data.why_the_gap.actions.length > 0 && (
            <>
              <div className="mt-2.5">
                <Eyebrow>Verified, held back — what would release it</Eyebrow>
              </div>
              <div className="mt-1.5 space-y-1.5">
                {data.why_the_gap.actions.map((action) => (
                  <Item
                    key={action.summary}
                    title={fromMinor(action.amount, currency)}
                    subtitle={`${action.count} item(s) · ${action.summary}`}
                  >
                    <p className="mt-0.5 text-[11px] leading-relaxed text-ink-2">{action.remedy}</p>
                  </Item>
                ))}
              </div>
            </>
          )}
        </div>
      )}

      {published.length > 0 && (
        <div className="mb-3">
          <Eyebrow>Published — click any figure to follow its evidence</Eyebrow>
          <div className="mt-1.5 space-y-1.5">
            {published.slice(0, 20).map((item) => (
              <EvidenceChain
                key={item.id}
                workspaceId={workspaceId}
                itemId={item.id}
                description={item.description}
                amount={item.amount}
              />
            ))}
          </div>
        </div>
      )}

      {withheld.length > 0 && (
        <>
          <Eyebrow>Withheld, with the reason</Eyebrow>
          <div className="mt-1.5 space-y-1.5">
            {withheld.slice(0, 20).map((item) => (
              <Item key={item.id} title={item.description} right={item.amount} tone="warn">
                <p className="mt-0.5 text-[11px] leading-relaxed text-ink-2">
                  {item.withheld_because}
                </p>
              </Item>
            ))}
          </div>
        </>
      )}
    </>
  );
}

/* --- decisions and tracing ------------------------------------------------- */

/**
 * A proposed merge, accepted or refused by a person.
 *
 * Feature 2 is asymmetric: merging four spellings of one company matters as much as
 * refusing to merge two companies whose names differ by a letter. So both answers are
 * first-class buttons, and the signals the matcher weighed are shown rather than a
 * single confidence number a reviewer cannot argue with.
 */
function MatchDecision({
  workspaceId,
  match,
  onDecided,
}: {
  workspaceId: string;
  match: MatchProposal;
  onDecided: () => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reason, setReason] = useState("");

  async function decide(decision: "accepted" | "rejected") {
    if (!reason.trim()) {
      setError("Say why. The reason is stored with the merge and travels into the audit log.");
      return;
    }
    setBusy(decision);
    setError(null);
    try {
      await api.decideMatch(workspaceId, match.id, decision, reason.trim());
      onDecided();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not record that decision");
    } finally {
      setBusy(null);
    }
  }

  return (
    <Item
      title={`${match.left.label} ↔ ${match.right.label}`}
      subtitle={`${Math.round((match.score ?? 0) * 100)}% · ${match.method}`}
      tone="warn"
    >
      {match.signals?.length > 0 && (
        <div className="mt-1 space-y-0.5">
          {match.signals.slice(0, 5).map((s) => (
            <div key={s.field} className="flex justify-between text-[10.5px]">
              <span className="text-ink-3">
                {s.field} — {s.outcome}
              </span>
              <span className="font-mono tnum text-ink-2">
                {s.weight > 0 ? "+" : ""}
                {s.weight.toFixed(2)}
              </span>
            </div>
          ))}
        </div>
      )}
      <input
        value={reason}
        onChange={(e) => setReason(e.target.value)}
        placeholder="Why? Recorded with the decision."
        className="mt-1.5 h-7 w-full rounded-[5px] border border-line px-2 text-[11px] placeholder:text-ink-3 focus:border-cobalt focus:outline-none"
      />
      {error && <p className="mt-1 text-[10.5px] text-rust">{error}</p>}
      <div className="mt-1.5 flex gap-1">
        <Button
          size="sm"
          className="flex-1"
          disabled={busy !== null || !reason.trim()}
          onClick={() => void decide("accepted")}
        >
          {busy === "accepted" ? <Spinner /> : "Same company"}
        </Button>
        <Button
          size="sm"
          className="flex-1"
          disabled={busy !== null || !reason.trim()}
          onClick={() => void decide("rejected")}
        >
          {busy === "rejected" ? <Spinner /> : "Different"}
        </Button>
      </div>
    </Item>
  );
}

/**
 * Was this indicator worth raising?
 *
 * The answer feeds the precision the anomaly engine measures on itself, and that
 * measurement decides whether the model is allowed to run at all next time. Marking a
 * finding useless is therefore a real input, not a dismissal.
 */
function AnomalyVerdict({
  workspaceId,
  anomaly,
  onChanged,
}: {
  workspaceId: string;
  anomaly: AnomalyFinding;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);

  async function mark(useful: boolean) {
    setBusy(true);
    try {
      await api.anomalyFeedback(workspaceId, anomaly.id, useful);
      onChanged();
    } finally {
      setBusy(false);
    }
  }

  if (anomaly.is_false_positive !== null && anomaly.is_false_positive !== undefined) {
    return (
      <p className="mt-1.5 text-[10.5px] text-ink-3">
        Marked {anomaly.is_false_positive ? "not useful" : "useful"} by a reviewer.
      </p>
    );
  }

  return (
    <div className="mt-1.5 flex items-center gap-1.5">
      <span className="text-[10.5px] text-ink-3">Worth raising?</span>
      <button
        disabled={busy}
        onClick={() => void mark(true)}
        className="rounded-[4px] px-1.5 py-[2px] text-[10.5px] text-emerald transition-colors hover:bg-emerald-soft disabled:opacity-50"
      >
        Yes
      </button>
      <button
        disabled={busy}
        onClick={() => void mark(false)}
        className="rounded-[4px] px-1.5 py-[2px] text-[10.5px] text-ink-2 transition-colors hover:bg-slate-soft disabled:opacity-50"
      >
        No
      </button>
    </div>
  );
}

/**
 * One published figure, and the chain of records behind it.
 *
 * `Customer → Contract → Invoice → Payment → Bank receipt` is the product's headline
 * claim, and a claim nobody can open is a slogan. A break in the chain is shown as the
 * finding it is rather than quietly producing a shorter list.
 */
function EvidenceChain({
  workspaceId,
  itemId,
  description,
  amount,
}: {
  workspaceId: string;
  itemId: string;
  description: string;
  amount: string;
}) {
  const [open, setOpen] = useState(false);
  const [trace, setTrace] = useState<EvidenceTrace | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function toggle() {
    const next = !open;
    setOpen(next);
    if (next && !trace) {
      try {
        setTrace(await api.traceEvidence(workspaceId, itemId));
      } catch (err) {
        setError(err instanceof Error ? err.message : "Could not follow this figure");
      }
    }
  }

  return (
    <div className="overflow-hidden rounded-[7px] border border-line">
      <button
        onClick={() => void toggle()}
        className="flex w-full items-start justify-between gap-2 px-2.5 py-2 text-left transition-colors hover:bg-cobalt-soft/40"
      >
        <span className="min-w-0">
          <span className="block truncate text-[12px] font-medium text-ink">{description}</span>
          <span className="text-[10.5px] text-cobalt">
            {open ? "Hide the chain" : "Follow the evidence"}
          </span>
        </span>
        <span className="shrink-0 font-mono tnum text-[11.5px] text-ink">{amount}</span>
      </button>

      {open && (
        <div className="border-t border-line-2 px-2.5 py-2">
          {error && <p className="text-[11px] text-rust">{error}</p>}
          {!trace && !error && <Spinner className="text-ink-3" />}
          {trace && (
            <>
              <ol className="space-y-0">
                {trace.nodes.map((node, index) => (
                  <li key={node.id} className="flex gap-2">
                    <span className="flex flex-col items-center">
                      <span className="mt-[5px] h-[7px] w-[7px] rounded-full bg-emerald" />
                      {index < trace.nodes.length - 1 && (
                        <span className="h-full min-h-[18px] w-px bg-line" />
                      )}
                    </span>
                    <span className="pb-2">
                      <span className="block text-[11.5px] font-medium capitalize text-ink">
                        {node.kind}
                      </span>
                      <span className="block text-[11px] leading-snug text-ink-2">
                        {node.label}
                      </span>
                      {typeof node.amount === "string" && (
                        <span className="block font-mono text-[10.5px] text-ink-3">
                          {node.amount}
                        </span>
                      )}
                    </span>
                  </li>
                ))}
              </ol>
              {trace.breaks?.length > 0 && (
                <div className="mt-1">
                  <Banner tone="warn">
                    {trace.breaks.join(" · ")}
                  </Banner>
                </div>
              )}
              {trace.critic && (
                <p className="mt-1.5 text-[10.5px] text-ink-3">
                  Critic verdict: {String(trace.critic.verdict ?? "—").toLowerCase()}
                </p>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}
