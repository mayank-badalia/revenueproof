"use client";

/**
 * Where a workspace gets its evidence — and the first screen a new user meets.
 *
 * The design problem this solves: four options written as four paragraphs read as
 * one grey wall, and a choice with no adjacent action leaves the user hunting for
 * the button that acts on it. Selecting "generated data" and being shown only a
 * seed box is a dead end.
 *
 * So each option states in one line what you get, and **the selected option opens
 * an action bar directly beneath it** carrying its own inputs and its own primary
 * button, labelled for what it will actually do — "Generate and load", not a
 * generic "Collect". Nothing is further away than the decision that needs it.
 *
 * The default is whatever this deployment can genuinely reach. On a machine with
 * connected accounts that is the real data: an operator demonstrating their own
 * account should not have to hunt for a setting to see it. With no credentials it
 * falls back to demonstration data, which is labelled synthetic everywhere it
 * appears and can never be mistaken for a real reconciliation.
 */

import { useState } from "react";
import { api } from "@/lib/api";
import type { ContractUploadResult, ConnectionStatus, IngestionRun } from "@/lib/types";

type Mode = "template" | "generated" | "upload" | "live";

const OPTIONS: {
  id: Mode;
  title: string;
  badge: string;
  summary: string;
  action: string;
  running: string;
  note: string;
}[] = [
  {
    id: "template",
    title: "Demonstration data",
    badge: "Instant · identical every run",
    summary: "Twenty invented companies with the awkward cases a real book has.",
    action: "Load demonstration data",
    running: "Loading…",
    note: "One customer spelled four ways, a one-time fee sold as a subscription, a payment refunded days later, an agent paying for two customers, money that arrives and leaves again. Identical every run, so every figure can be checked against a known answer.",
  },
  {
    id: "generated",
    title: "Generated demonstration data",
    badge: "Instant · new companies each seed",
    summary: "The same awkward cases under companies nobody has ever seen.",
    action: "Generate and load",
    running: "Generating…",
    note: "New names, spellings, domains, tax identifiers and cities, built from your seed. This is the honest test of whether the product works or merely works on the demo — pick any seed and watch every detector still fire on companies that did not exist a moment ago.",
  },
  {
    id: "upload",
    title: "Upload your own records",
    badge: "Your data · no account access",
    summary: "A bank statement as CSV, and contracts as PDFs.",
    action: "Go to the bank-statement upload",
    running: "…",
    note: "No credentials are asked for. Files go through exactly the same parsers the live connectors use; column names are matched flexibly, and any row that cannot be read is quarantined and shown to you rather than dropped.",
  },
  {
    id: "live",
    title: "Connect your own accounts",
    badge: "Your data · read-only",
    summary: "Razorpay, Zoho Books, HubSpot and Google Drive.",
    action: "Pull from connected accounts",
    running: "Collecting…",
    note: "Read access is enough for all four; the app never writes to your systems. Tokens are encrypted before storage and are never included in any prompt sent to the language model. None of the four is a bank, so no demonstration statement is mixed in and receipts stop at “verified by the processor” until you upload the bank statement covering this period — that upload is what lets a figure become bank-confirmed.",
  },
];

const PROVIDERS = [
  { id: "razorpay", name: "Razorpay", blurb: "Payments, refunds, disputes, settlements" },
  { id: "zoho_books", name: "Zoho Books", blurb: "Invoices, credit notes, customers" },
  { id: "hubspot", name: "HubSpot", blurb: "Companies and contacts" },
  { id: "google_drive", name: "Google Drive", blurb: "Contract PDFs, read-only" },
];

const SEED_WORDS = [
  "acme-demo", "harbour-run", "northwind", "silverpine", "kestrel-2027",
  "bluefield", "ironvale", "cobalt-run", "meridian-x", "tallgrass",
];

export function DataSourcePanel({
  workspaceId,
  companyName,
  connections,
  deploymentProviders,
  onChanged,
}: {
  workspaceId: string;
  companyName: string;
  connections: ConnectionStatus[];
  deploymentProviders?: Record<string, boolean>;
  onChanged?: () => void;
}) {
  // Capability, not "what did the last run happen to serve". A deployment holding
  // credentials can reach those accounts even if the most recent fetch was
  // demonstration data — conflating the two hid four live accounts behind a
  // disabled option the moment anyone pressed the demo button.
  const reachable = Object.entries(deploymentProviders ?? {})
    .filter(([, ok]) => ok)
    .map(([name]) => name);
  const connectedNow = connections.filter((c) => c.is_active && !c.is_synthetic);
  const liveCount = Math.max(reachable.length, connectedNow.length);
  const canGoLive = liveCount > 0;

  // The demonstration set is the default even where live accounts are reachable.
  // Defaulting to "live" meant the first button a visitor pressed pulled four real
  // accounts that have no bank feed between them — and with no bank statement no
  // receipt can be corroborated, so the headline came out at zero on a product
  // whose entire job is to produce that number. The demonstration set is coherent
  // across all five sources, so it proves what the tool does; reaching a real
  // account is a deliberate second step, and says plainly what it still needs.
  const [mode, setMode] = useState<Mode>("template");
  const [seed, setSeed] = useState("acme-demo");
  const [busy, setBusy] = useState(false);
  const [run, setRun] = useState<IngestionRun | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [downloading, setDownloading] = useState(false);
  const [connecting, setConnecting] = useState<string | null>(null);
  const [tokens, setTokens] = useState<Record<string, string>>({});

  const option = OPTIONS.find((entry) => entry.id === mode) ?? OPTIONS[0];

  async function act() {
    if (mode === "upload") {
      // The bank statement lives in the evidence vault; contract PDFs upload from
      // the control just below this button.
      document
        .getElementById("evidence-vault")
        ?.scrollIntoView({ behavior: "smooth", block: "center" });
      return;
    }
    setBusy(true);
    setError(null);
    try {
      setRun(
        await api.runIngestion(
          workspaceId,
          mode === "template" || mode === "generated",
          mode === "generated" ? seed : undefined,
        ),
      );
      onChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not collect evidence");
    } finally {
      setBusy(false);
    }
  }

  async function downloadDataset() {
    setDownloading(true);
    setError(null);
    try {
      await api.downloadDemoDataset(mode === "generated" ? seed : undefined);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not download the dataset");
    } finally {
      setDownloading(false);
    }
  }

  async function downloadReport() {
    setDownloading(true);
    setError(null);
    try {
      await api.downloadReport(workspaceId);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not build the report");
    } finally {
      setDownloading(false);
    }
  }

  async function connect(providerId: string) {
    const token = (tokens[providerId] ?? "").trim();
    if (!token) return;
    setBusy(true);
    setError(null);
    try {
      await api.connectSource(workspaceId, providerId, token);
      // Clear it the moment it is stored: no reason for a credential to linger in
      // component state after it has been sent.
      setTokens((current) => ({ ...current, [providerId]: "" }));
      setConnecting(null);
      setMode("live");
      onChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save the connection");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section id="evidence-sources" className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
      <div>
        <h2 className="text-sm font-semibold">Evidence sources</h2>
        <p className="mt-0.5 max-w-2xl text-xs text-slate-600">
          Choose where this workspace gets its records. Synthetic sources are
          labelled as such everywhere they appear, so a demonstration can never be
          mistaken for a real reconciliation.
        </p>
      </div>

      {error && (
        <p role="alert" className="mt-3 rounded-md bg-rose-50 px-3 py-2 text-xs text-rose-700">
          {error}
        </p>
      )}

      {canGoLive && (
        <p className="mt-3 rounded-md bg-emerald-50 px-3 py-2 text-[11px] text-emerald-900">
          <span className="font-medium">
            {liveCount} account{liveCount > 1 ? "s" : ""} already connected on this
            deployment
          </span>{" "}
          — nothing to sign in to.
        </p>
      )}

      {/* --- the four choices, one line each ------------------------------- */}
      <div className="mt-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        {OPTIONS.map((entry) => {
          const active = mode === entry.id;
          const disabled = entry.id === "live" && !canGoLive;
          return (
            <button
              key={entry.id}
              type="button"
              onClick={() => setMode(entry.id)}
              disabled={disabled}
              aria-pressed={active}
              className={`rounded-md border p-3 text-left transition ${
                active
                  ? "border-slate-900 bg-slate-900 text-white shadow-sm"
                  : "border-slate-200 hover:border-slate-400"
              } ${disabled ? "cursor-not-allowed opacity-40" : ""}`}
            >
              <p className="text-xs font-semibold">{entry.title}</p>
              <p
                className={`mt-0.5 text-[10px] uppercase tracking-wide ${
                  active ? "text-slate-300" : "text-slate-500"
                }`}
              >
                {entry.badge}
              </p>
              <p
                className={`mt-1.5 text-[11px] ${
                  active ? "text-slate-100" : "text-slate-600"
                }`}
              >
                {entry.summary}
              </p>
              {disabled && (
                <p className="mt-1 text-[10px] text-amber-700">
                  No credentials on this deployment.
                </p>
              )}
            </button>
          );
        })}
      </div>

      {/* --- the action for whatever is selected, right here --------------- */}
      <div className="mt-3 rounded-md border border-slate-300 bg-slate-50 p-3">
        <p className="text-[11px] text-slate-700">{option.note}</p>

        {mode === "generated" && (
          <div className="mt-2.5 flex flex-wrap items-center gap-2">
            <label htmlFor="seed" className="text-[11px] font-medium text-slate-700">
              Seed
            </label>
            <input
              id="seed"
              value={seed}
              onChange={(event) => setSeed(event.target.value)}
              placeholder="any text"
              className="rounded border border-slate-300 px-2 py-1 text-[11px]"
            />
            <button
              type="button"
              onClick={() =>
                setSeed(SEED_WORDS[Math.floor(Math.random() * SEED_WORDS.length)])
              }
              className="rounded border border-slate-300 bg-white px-2 py-1 text-[11px] text-slate-700 hover:bg-slate-50"
            >
              Surprise me
            </button>
            <span className="text-[10px] text-slate-500">
              The same seed always produces the same companies.
            </span>
          </div>
        )}

        {/* Only the controls this option actually uses. Showing all four options'
            buttons at once made every choice look like it did the same thing, and
            put a "connect your own accounts" panel under "upload your own files" —
            which is the one place a reader is being told no credentials are
            needed. */}
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={act}
            disabled={busy}
            className="rounded-md bg-slate-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-slate-800 disabled:opacity-50"
          >
            {busy ? option.running : option.action}
          </button>

          {(mode === "template" || mode === "generated") && (
            <button
              type="button"
              onClick={downloadDataset}
              disabled={downloading}
              className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs text-slate-700 hover:bg-slate-50 disabled:opacity-50"
            >
              {downloading ? "Preparing…" : "Download this dataset (.zip)"}
            </button>
          )}
        </div>

        {(mode === "template" || mode === "generated") && (
          <p className="mt-1.5 text-[10px] text-slate-500">
            The download includes the bank statement as CSV — re-uploadable through
            the normal path, so you can check the parser on the same file the demo
            used. Loading a dataset <strong>replaces</strong> whatever this workspace
            held before, so two demos never end up mixed into one set of books.
          </p>
        )}

        {mode === "upload" && (
          <ContractUpload workspaceId={workspaceId} onChanged={onChanged} />
        )}
      </div>

      {run && (
        <p className="mt-3 rounded-md bg-emerald-50 px-3 py-2 text-[11px] text-emerald-900">
          Collected {run.total_canonical} records across{" "}
          {Object.keys(run.sources).length} sources.
        </p>
      )}

      {/* --- connections ---------------------------------------------------
          Only under "connect your own accounts". Under the other three options it
          answered a question nobody had asked, and directly contradicted the
          upload option's promise that no credentials are needed. */}
      <div className={mode === "live" ? "mt-5" : "hidden"}>
        <h3 className="text-xs font-semibold text-slate-700">Connected accounts</h3>
        <p className="mt-0.5 text-[11px] text-slate-500">
          Read-only access is enough for every provider. Tokens are encrypted before
          storage and are never sent to the language model.
        </p>
        {liveCount > 0 && (
          <p className="mt-2 rounded-md border border-sky-200 bg-sky-50 px-3 py-2 text-[11px] text-sky-900">
            <strong>These are the demo account&rsquo;s connections.</strong> The four
            providers below are already signed in on this deployment, so you can run
            a full verification without connecting anything — the accounts are real
            and read-only, and they belong to the demo, not to you.{" "}
            <strong>You are free to connect your own instead:</strong> use{" "}
            <em>Replace</em> on any provider to point it at your own Razorpay, Zoho
            Books, HubSpot or Google Drive. Doing so affects only this workspace.
          </p>
        )}
        <div className="mt-2 grid gap-2 sm:grid-cols-2">
          {PROVIDERS.map((provider) => {
            const live =
              reachable.includes(provider.id) ||
              connections.some(
                (c) =>
                  c.source_system === provider.id && c.is_active && !c.is_synthetic,
              );
            return (
              <div key={provider.id} className="rounded border border-slate-200 p-3">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <p className="text-xs font-medium">
                      {provider.name}
                      <span
                        className={`ml-2 rounded px-1.5 py-0.5 text-[10px] ${
                          live
                            ? "bg-emerald-100 text-emerald-800"
                            : "bg-slate-100 text-slate-600"
                        }`}
                      >
                        {live ? "connected" : "not connected"}
                      </span>
                    </p>
                    <p className="mt-0.5 text-[11px] text-slate-600">
                      {provider.blurb}
                    </p>
                  </div>
                  <button
                    type="button"
                    onClick={() =>
                      setConnecting(connecting === provider.id ? null : provider.id)
                    }
                    className="shrink-0 rounded border border-slate-300 px-2.5 py-1 text-[11px] text-slate-700 hover:bg-slate-50"
                  >
                    {live ? "Replace" : "Connect"}
                  </button>
                </div>

                {connecting === provider.id && (
                  <form
                    className="mt-2 space-y-1.5 border-t border-slate-100 pt-2"
                    onSubmit={(event) => {
                      event.preventDefault();
                      connect(provider.id);
                    }}
                  >
                    <input
                      type="password"
                      autoComplete="off"
                      value={tokens[provider.id] ?? ""}
                      onChange={(event) =>
                        setTokens((current) => ({
                          ...current,
                          [provider.id]: event.target.value,
                        }))
                      }
                      placeholder={`${provider.name} access token or API key`}
                      className="w-full rounded border border-slate-300 px-2 py-1 text-[11px]"
                    />
                    <div className="flex items-center gap-2">
                      <button
                        type="submit"
                        disabled={busy || !(tokens[provider.id] ?? "").trim()}
                        className="rounded bg-slate-900 px-2.5 py-1 text-[11px] text-white disabled:opacity-40"
                      >
                        Save connection
                      </button>
                      <span className="text-[10px] text-slate-500">
                        Encrypted before storage · never sent to the model
                      </span>
                    </div>
                  </form>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </section>
  );
}

/**
 * Contract PDFs, uploaded here rather than promised and not delivered.
 *
 * The option said "a bank statement as CSV **and contracts as PDFs**" and only the
 * statement had anywhere to go — the button scrolled to the CSV input and nothing
 * on the page accepted a PDF at all. A workspace built from a founder's own records
 * therefore had every contract unread, which produces an ARR figure with no contract
 * behind it: the one outcome this product exists to prevent.
 */
function ContractUpload({
  workspaceId,
  onChanged,
}: {
  workspaceId: string;
  onChanged?: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ContractUploadResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function upload(event: React.ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.target.files ?? []);
    if (files.length === 0) return;
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      setResult(await api.uploadContracts(workspaceId, files));
      onChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setBusy(false);
      event.target.value = "";
    }
  }

  return (
    <div className="mt-3 rounded-md border border-slate-300 bg-white p-3">
      <p className="text-xs font-medium">Contracts (PDF)</p>
      <p className="mt-0.5 text-[11px] text-slate-600">
        Read by the same extractor as any other contract, with a verified page
        citation behind every amount. A file whose first bytes are not a PDF is
        refused and shown to you — the extension is chosen by whoever sends the
        file, so it is not what gets checked.
      </p>
      <label className="mt-2 inline-block cursor-pointer rounded-md bg-slate-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-slate-800">
        {busy ? "Uploading…" : "Choose contract PDFs"}
        <input
          type="file"
          accept="application/pdf,.pdf"
          multiple
          onChange={upload}
          disabled={busy}
          className="hidden"
        />
      </label>

      {error && (
        <p role="alert" className="mt-2 rounded bg-rose-50 px-2 py-1.5 text-[11px] text-rose-700">
          {error}
        </p>
      )}

      {result && (
        <div className="mt-2 space-y-1">
          {result.accepted.map((file) => (
            <p key={file.filename} className="text-[11px] text-emerald-800">
              ✓ {file.filename} —{" "}
              {file.outcome === "duplicate"
                ? "already vaulted; not added twice"
                : "vaulted and ready to read"}
            </p>
          ))}
          {result.rejected.map((file) => (
            <p key={file.filename} className="text-[11px] text-rose-700">
              ✗ {file.filename} — {file.reason}
            </p>
          ))}
        </div>
      )}
    </div>
  );
}
