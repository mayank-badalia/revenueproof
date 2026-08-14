"use client";

/**
 * Creating a workspace is stating a claim. The form says so.
 *
 * Two steps rather than one long column: the first captures the claim being tested,
 * the second explains what happens next. Splitting them keeps the second screen
 * honest — it is the only place to say "you get an empty canvas", which is a
 * surprising outcome unless someone tells you it is deliberate.
 */

import { useState } from "react";

import { api, ApiError } from "@/lib/api";
import { formatAmountInput, plainAmount } from "@/lib/format";
import { Banner, Button, Field, Input, Spinner } from "@/components/ui/primitives";
import type { Workspace } from "@/lib/types";

const CURRENCIES = ["INR", "USD", "EUR", "GBP", "SGD", "AED"];

type ClaimType = "revenue" | "arr" | "cash";

const CLAIM_TYPES: { id: ClaimType; label: string; hint: string }[] = [
  { id: "revenue", label: "Revenue", hint: "Total recognised in the period" },
  { id: "arr", label: "ARR", hint: "Annualised recurring only" },
  { id: "cash", label: "Cash received", hint: "Money that actually landed" },
];

export function CreateWorkspaceDialog({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (workspace: Workspace) => void;
}) {
  const [step, setStep] = useState<1 | 2>(1);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [form, setForm] = useState({
    name: "",
    company: "",
    start: "2026-04-01",
    end: "2027-03-31",
    currency: "INR",
    claimed: "1,50,00,000",
    claimedArr: "48,00,000",
    claimType: "revenue" as ClaimType,
    description: "",
  });

  if (!open) return null;

  const set = <K extends keyof typeof form>(key: K, value: (typeof form)[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }));

  const step1Valid = form.company.trim().length > 1 && form.start && form.end;

  async function create() {
    setBusy(true);
    setError(null);
    try {
      const workspace = await api.createWorkspace({
        company_name: form.company.trim(),
        legal_name: form.name.trim() || null,
        reporting_period_start: form.start,
        reporting_period_end: form.end,
        base_currency: form.currency,
        claimed_revenue: plainAmount(form.claimed),
        claimed_arr: plainAmount(form.claimedArr),
      });
      onCreated(workspace);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not create the workspace");
      setStep(1);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-navy-900/35 p-6 pt-[7vh]">
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Create verification workspace"
        className="w-full max-w-[520px] rounded-[12px] border border-line bg-paper shadow-[0_20px_60px_-12px_rgba(8,17,31,0.28)]"
      >
        <header className="flex items-start justify-between gap-4 border-b border-line px-5 py-4">
          <div>
            <h2 className="text-[15px] font-semibold tracking-[-0.01em] text-ink">
              Create verification workspace
            </h2>
            <p className="mt-0.5 text-[12.5px] text-ink-2">
              A workspace holds one claim, for one period, and the evidence that tests it.
            </p>
          </div>
          <button
            onClick={onClose}
            aria-label="Close"
            className="-mr-1 -mt-1 rounded p-1.5 text-ink-3 transition-colors hover:bg-slate-soft hover:text-ink"
          >
            <svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden>
              <path d="m4 4 8 8M12 4l-8 8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
            </svg>
          </button>
        </header>

        <div className="flex items-center gap-2 border-b border-line px-5 py-2.5">
          {[
            { n: 1, label: "The claim" },
            { n: 2, label: "What happens next" },
          ].map(({ n, label }, index) => (
            <div key={n} className="flex flex-1 items-center gap-2">
              <span
                className={`grid h-[18px] w-[18px] shrink-0 place-items-center rounded-full font-mono text-[10px] font-semibold ${
                  step >= n ? "bg-cobalt text-white" : "bg-slate-soft text-ink-3"
                }`}
              >
                {n}
              </span>
              <span
                className={`text-[12px] ${step >= n ? "font-medium text-ink" : "text-ink-3"}`}
              >
                {label}
              </span>
              {index === 0 && <span className="h-px flex-1 bg-line" />}
            </div>
          ))}
        </div>

        <div className="px-5 py-4">
          {error && (
            <div className="mb-3">
              <Banner tone="error">{error}</Banner>
            </div>
          )}

          {step === 1 ? (
            <div className="space-y-3.5">
              <Field label="Company under review">
                <Input
                  autoFocus
                  value={form.company}
                  onChange={(e) => set("company", e.target.value)}
                  placeholder="Northstar Technologies Private Limited"
                />
              </Field>

              <Field label="Workspace name" hint="Optional. Defaults to the company name.">
                <Input
                  value={form.name}
                  onChange={(e) => set("name", e.target.value)}
                  placeholder="Northstar FY26"
                />
              </Field>

              <div className="grid grid-cols-[1fr_1fr_110px] gap-3">
                <Field label="Period start">
                  <Input type="date" value={form.start} onChange={(e) => set("start", e.target.value)} />
                </Field>
                <Field label="Period end">
                  <Input type="date" value={form.end} onChange={(e) => set("end", e.target.value)} />
                </Field>
                <Field label="Currency">
                  <select
                    value={form.currency}
                    onChange={(e) => set("currency", e.target.value)}
                    className="h-9 w-full rounded-[7px] border border-line bg-paper px-2.5 text-[13.5px] text-ink focus:border-cobalt focus:outline-none focus:ring-2 focus:ring-cobalt/15"
                  >
                    {CURRENCIES.map((c) => (
                      <option key={c}>{c}</option>
                    ))}
                  </select>
                </Field>
              </div>

              <div className="grid grid-cols-2 gap-3">
                <Field label="Claimed revenue">
                  <Input
                    className="font-mono tnum"
                    value={form.claimed}
                    onChange={(e) => set("claimed", formatAmountInput(e.target.value))}
                    inputMode="decimal"
                  />
                </Field>
                <Field label="Claimed ARR">
                  <Input
                    className="font-mono tnum"
                    value={form.claimedArr}
                    onChange={(e) => set("claimedArr", formatAmountInput(e.target.value))}
                    inputMode="decimal"
                  />
                </Field>
              </div>

              <Field label="Claim type" hint="Which figure the verification is measured against.">
                <div className="flex gap-1.5">
                  {CLAIM_TYPES.map((type) => (
                    <button
                      key={type.id}
                      type="button"
                      onClick={() => set("claimType", type.id)}
                      title={type.hint}
                      className={`h-9 flex-1 rounded-[7px] text-[12.5px] font-medium transition-colors ${
                        form.claimType === type.id
                          ? "bg-cobalt text-white"
                          : "bg-paper text-ink-2 ring-1 ring-line hover:bg-slate-soft"
                      }`}
                    >
                      {type.label}
                    </button>
                  ))}
                </div>
              </Field>

              <Field label="Notes" hint="Optional. Anything a reviewer should know about this claim.">
                <textarea
                  value={form.description}
                  onChange={(e) => set("description", e.target.value)}
                  rows={2}
                  className="w-full resize-none rounded-[7px] border border-line bg-paper px-3 py-2 text-[13.5px] text-ink placeholder:text-ink-3 focus:border-cobalt focus:outline-none focus:ring-2 focus:ring-cobalt/15"
                  placeholder="Series A diligence, figures from the data room."
                />
              </Field>
            </div>
          ) : (
            <div className="space-y-3">
              <Banner tone="info">
                The workspace opens on an empty canvas with a single required node:{" "}
                <strong className="font-semibold">Load Evidence</strong>. Nothing can be
                verified until evidence is loaded.
              </Banner>
              <ol className="space-y-2.5 pt-1">
                {[
                  ["Load evidence", "Connect a demo dataset, your own accounts, or upload records."],
                  ["Run all", "Every remaining node is created, connected and run in dependency order."],
                  ["Review and publish", "Settle what the agents could not, then freeze the position."],
                ].map(([title, body], index) => (
                  <li key={title} className="flex gap-3">
                    <span className="mt-0.5 grid h-[19px] w-[19px] shrink-0 place-items-center rounded-full bg-slate-soft font-mono text-[10px] font-semibold text-ink-2">
                      {index + 1}
                    </span>
                    <span>
                      <span className="block text-[13px] font-medium text-ink">{title}</span>
                      <span className="block text-[12.5px] leading-relaxed text-ink-2">{body}</span>
                    </span>
                  </li>
                ))}
              </ol>
              <dl className="mt-4 rounded-[8px] bg-slate-soft px-3.5 py-3">
                {[
                  ["Company", form.company || "—"],
                  ["Period", `${form.start} to ${form.end}`],
                  ["Claim", `${form.currency} ${form.claimed}`],
                ].map(([k, v]) => (
                  <div key={k} className="flex justify-between gap-4 py-0.5">
                    <dt className="text-[12px] text-ink-2">{k}</dt>
                    <dd className="truncate font-mono tnum text-[12px] text-ink">{v}</dd>
                  </div>
                ))}
              </dl>
            </div>
          )}
        </div>

        <footer className="flex items-center justify-between gap-3 border-t border-line px-5 py-3.5">
          <Button variant="ghost" onClick={step === 1 ? onClose : () => setStep(1)}>
            {step === 1 ? "Cancel" : "Back"}
          </Button>
          {step === 1 ? (
            <Button variant="primary" disabled={!step1Valid} onClick={() => setStep(2)}>
              Continue
            </Button>
          ) : (
            <Button
              variant="primary"
              disabled={busy}
              onClick={create}
              icon={busy ? <Spinner /> : undefined}
            >
              {busy ? "Creating…" : "Create workspace"}
            </Button>
          )}
        </footer>
      </div>
    </div>
  );
}
