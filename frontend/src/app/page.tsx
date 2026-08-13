"use client";

/**
 * Workspace list and setup (§10.1).
 *
 * The claim a founder enters here is the claim every later feature tests, so the
 * form is the entry point to the whole product.
 */

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { ApiError, api, getToken, setToken } from "@/lib/api";
import type { Workspace } from "@/lib/types";
import { AuthGate } from "@/components/AuthGate";
import { ServiceStatus } from "@/components/ServiceStatus";

const CURRENCIES = ["INR", "USD", "EUR", "GBP", "SGD", "AED"];

/** Defaults to the Indian financial year, the common case for the target users. */
function defaultPeriod() {
  const now = new Date();
  const year = now.getMonth() >= 3 ? now.getFullYear() : now.getFullYear() - 1;
  return { start: `${year}-04-01`, end: `${year + 1}-03-31` };
}

/**
 * Group the integer part in the Indian system: 15000000 -> 1,50,00,000.
 *
 * Typed into a bare input, a crore is nine characters of unbroken digits and
 * nobody can tell 1,50,00,000 from 15,00,00,000 at a glance — least of all the
 * person entering the figure their whole report will be measured against.
 */
function groupIndian(digits: string): string {
  if (digits.length <= 3) return digits;
  const head = digits.slice(0, -3);
  const tail = digits.slice(-3);
  const parts: string[] = [];
  let rest = head;
  while (rest.length > 2) {
    parts.unshift(rest.slice(-2));
    rest = rest.slice(0, -2);
  }
  if (rest) parts.unshift(rest);
  return `${parts.join(",")},${tail}`;
}

/** Display value for a money input, keeping at most one decimal point. */
function formatAmountInput(raw: string): string {
  const cleaned = raw.replace(/[^\d.]/g, "");
  const [whole = "", ...rest] = cleaned.split(".");
  const fraction = rest.join("").slice(0, 2);
  const grouped = groupIndian(whole.replace(/^0+(?=\d)/, ""));
  return cleaned.includes(".") ? `${grouped}.${fraction}` : grouped;
}

/** Strip the grouping before it goes to the API, which wants a plain decimal. */
function plainAmount(display: string): string {
  return display.replace(/,/g, "") || "0";
}

//: The §15 dataset retains 1,39,83,000 of cash against its invoices. A claim a
//: little above that is what a founder with these books would actually assert, and
//: it puts the demonstration at ~93% proven rather than ~123%, which is what a
//: claim *below* the evidence produces.
const DEFAULT_CLAIMED_REVENUE = "1,50,00,000";
const DEFAULT_CLAIMED_ARR = "48,00,000";

export default function HomePage() {
  const [authed, setAuthed] = useState<boolean | null>(null);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [showForm, setShowForm] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const period = defaultPeriod();
  const [form, setForm] = useState({
    company_name: "",
    legal_name: "",
    reporting_period_start: period.start,
    reporting_period_end: period.end,
    base_currency: "INR",
    // Prefilled to what the demonstration dataset's evidence actually supports,
    // slightly overstated — which is the situation this product exists for. Left
    // empty it defaulted to zero, and a claim of zero makes every verified rupee
    // read as "evidence beyond the claim" and the headline percentage meaningless.
    claimed_revenue: DEFAULT_CLAIMED_REVENUE,
    claimed_arr: DEFAULT_CLAIMED_ARR,
    materiality_threshold_pct: 1.0,
    accounting_method: "accrual",
  });

  const load = useCallback(async () => {
    try {
      setWorkspaces(await api.listWorkspaces());
      setError(null);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setAuthed(false);
        return;
      }
      setError(err instanceof Error ? err.message : "Failed to load workspaces");
    }
  }, []);

  useEffect(() => {
    if (!getToken()) {
      setAuthed(false);
      return;
    }
    setAuthed(true);
    load();
  }, [load]);

  async function createWorkspace(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const created = await api.createWorkspace({
        ...form,
        legal_name: form.legal_name || null,
        claimed_revenue: plainAmount(form.claimed_revenue),
        claimed_arr: plainAmount(form.claimed_arr),
      });
      setWorkspaces((current) => [created, ...current]);
      setShowForm(false);
      setForm({
        ...form,
        company_name: "",
        legal_name: "",
        claimed_revenue: DEFAULT_CLAIMED_REVENUE,
        claimed_arr: DEFAULT_CLAIMED_ARR,
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not create workspace");
    } finally {
      setBusy(false);
    }
  }

  if (authed === null) return null;
  if (!authed) {
    return (
      <AuthGate
        onAuthenticated={() => {
          setAuthed(true);
          load();
        }}
      />
    );
  }

  return (
    <main className="mx-auto max-w-5xl px-4 py-8">
      <header className="mb-6 flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">RevenueProof</h1>
          <p className="mt-1 text-sm text-slate-600">
            Every verified rupee is traceable. Every unsupported rupee is visible.
          </p>
        </div>
        <button
          type="button"
          onClick={() => {
            setToken(null);
            setAuthed(false);
          }}
          className="rounded-md border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-100"
        >
          Sign out
        </button>
      </header>

      <div className="mb-6">
        <ServiceStatus />
      </div>

      {error && (
        <p role="alert" className="mb-4 rounded-md bg-rose-50 px-3 py-2 text-sm text-rose-700">
          {error}
        </p>
      )}

      <div className="mb-4 flex items-center justify-between">
        <h2 className="text-lg font-medium">Review workspaces</h2>
        <button
          type="button"
          onClick={() => setShowForm((v) => !v)}
          className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-800"
        >
          {showForm ? "Cancel" : "New workspace"}
        </button>
      </div>

      {showForm && (
        <form
          onSubmit={createWorkspace}
          className="mb-6 grid gap-4 rounded-lg border border-slate-200 bg-white p-5 shadow-sm sm:grid-cols-2"
        >
          <div className="sm:col-span-2">
            <label htmlFor="company_name" className="block text-sm font-medium">
              Company name <span className="text-rose-600">*</span>
            </label>
            <input
              id="company_name"
              required
              value={form.company_name}
              onChange={(e) => setForm({ ...form, company_name: e.target.value })}
              placeholder="Northstar Technologies Private Limited"
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
            />
          </div>

          <div>
            <label htmlFor="period_start" className="block text-sm font-medium">
              Reporting period start
            </label>
            <input
              id="period_start"
              type="date"
              required
              value={form.reporting_period_start}
              onChange={(e) => setForm({ ...form, reporting_period_start: e.target.value })}
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
            />
          </div>

          <div>
            <label htmlFor="period_end" className="block text-sm font-medium">
              Reporting period end
            </label>
            <input
              id="period_end"
              type="date"
              required
              value={form.reporting_period_end}
              onChange={(e) => setForm({ ...form, reporting_period_end: e.target.value })}
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
            />
          </div>

          <div>
            <label htmlFor="currency" className="block text-sm font-medium">
              Reporting currency
            </label>
            <select
              id="currency"
              value={form.base_currency}
              onChange={(e) => setForm({ ...form, base_currency: e.target.value })}
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
            >
              {CURRENCIES.map((code) => (
                <option key={code} value={code}>
                  {code}
                </option>
              ))}
            </select>
          </div>

          <div>
            <label htmlFor="accounting_method" className="block text-sm font-medium">
              Accounting method
            </label>
            <select
              id="accounting_method"
              value={form.accounting_method}
              onChange={(e) => setForm({ ...form, accounting_method: e.target.value })}
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
            >
              <option value="accrual">Accrual</option>
              <option value="cash">Cash</option>
            </select>
          </div>

          <div>
            <label htmlFor="claimed_revenue" className="block text-sm font-medium">
              Claimed revenue
            </label>
            <input
              id="claimed_revenue"
              inputMode="decimal"
              value={form.claimed_revenue}
              onChange={(e) =>
                setForm({ ...form, claimed_revenue: formatAmountInput(e.target.value) })
              }
              placeholder="1,50,00,000"
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
            />
            <p className="mt-1 text-xs text-slate-500">
              The figure being tested, not an accepted fact.
            </p>
          </div>

          <div>
            <label htmlFor="claimed_arr" className="block text-sm font-medium">
              Claimed ARR
            </label>
            <input
              id="claimed_arr"
              inputMode="decimal"
              value={form.claimed_arr}
              onChange={(e) =>
                setForm({ ...form, claimed_arr: formatAmountInput(e.target.value) })
              }
              placeholder="1,50,00,000"
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
            />
          </div>

          <div>
            <label htmlFor="materiality" className="block text-sm font-medium">
              Materiality threshold (%)
            </label>
            <input
              id="materiality"
              type="number"
              step="0.1"
              min="0.1"
              max="100"
              value={form.materiality_threshold_pct}
              onChange={(e) =>
                setForm({ ...form, materiality_threshold_pct: Number(e.target.value) })
              }
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
            />
            <p className="mt-1 text-xs text-slate-500">
              Items above this share of the claim need critic agreement.
            </p>
          </div>

          <div className="sm:col-span-2">
            <button
              type="submit"
              disabled={busy}
              className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-50"
            >
              {busy ? "Creating…" : "Create workspace"}
            </button>
          </div>
        </form>
      )}

      {workspaces.length === 0 ? (
        <p className="rounded-lg border border-dashed border-slate-300 bg-white p-8 text-center text-sm text-slate-500">
          No workspaces yet. Create one to begin a revenue review.
        </p>
      ) : (
        <ul className="grid gap-3">
          {workspaces.map((workspace) => (
            <li key={workspace.id}>
              <Link
                href={`/workspaces/${workspace.id}`}
                className="block rounded-lg border border-slate-200 bg-white p-4 shadow-sm transition hover:border-slate-400"
              >
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <h3 className="font-medium">{workspace.company_name}</h3>
                    <p className="mt-0.5 text-xs text-slate-500">
                      {workspace.reporting_period_start} → {workspace.reporting_period_end} ·{" "}
                      {workspace.accounting_method}
                    </p>
                  </div>
                  <dl className="flex gap-6 text-right text-sm">
                    <div>
                      <dt className="text-xs text-slate-500">Claimed revenue</dt>
                      <dd className="font-medium tabular-nums">
                        {workspace.claimed_revenue.display}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-xs text-slate-500">Claimed ARR</dt>
                      <dd className="font-medium tabular-nums">
                        {workspace.claimed_arr.display}
                      </dd>
                    </div>
                  </dl>
                </div>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
