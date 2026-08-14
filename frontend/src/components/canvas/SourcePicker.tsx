"use client";

/**
 * Choosing where a workspace's evidence comes from.
 *
 * The demonstration set is first and is the default, because it is coherent across
 * all five sources — including a bank statement, which none of the four connectable
 * providers is. Reaching a real account is a deliberate second step and says plainly
 * what it still needs, rather than silently producing a workspace that can never
 * prove anything.
 */

import { useState } from "react";

import { api, ApiError } from "@/lib/api";
import { Banner, Button, Spinner } from "@/components/ui/primitives";
import type { ConnectionStatus } from "@/lib/types";

type SourceId = "template" | "generated" | "live" | "bank" | "contracts";

const SEEDS = ["acme-demo", "harbour-run", "northwind", "silverpine", "kestrel-2027"];

export function SourcePicker({
  workspaceId,
  connections,
  onLoaded,
  onClose,
}: {
  workspaceId: string;
  connections: ConnectionStatus[];
  onLoaded: () => void;
  onClose: () => void;
}) {
  const [choice, setChoice] = useState<SourceId>("template");
  const [seed, setSeed] = useState(SEEDS[0]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const liveCount = connections.filter((c) => c.is_active && !c.is_synthetic).length;

  const options: {
    id: SourceId;
    name: string;
    detail: string;
    badge?: string;
    badgeTone?: "ready" | "muted";
  }[] = [
    {
      id: "template",
      name: "Demonstration data",
      detail: "Twenty invented companies, all five sources, identical every run",
      badge: "Ready",
      badgeTone: "ready",
    },
    {
      id: "generated",
      name: "Generated demonstration data",
      detail: "The same awkward cases under companies nobody has seen",
      badge: "Ready",
      badgeTone: "ready",
    },
    {
      id: "live",
      name: "Connected accounts",
      detail: "Razorpay, Zoho Books, HubSpot and Google Drive — read-only",
      badge: liveCount > 0 ? `${liveCount} connected` : "None connected",
      badgeTone: liveCount > 0 ? "ready" : "muted",
    },
    {
      id: "bank",
      name: "Bank statement",
      detail: "Upload a CSV. This is what turns a receipt into bank-confirmed.",
    },
    {
      id: "contracts",
      name: "Contracts and invoices",
      detail: "Upload PDFs. Read by the same extractor as connected documents.",
    },
  ];

  async function load(files?: FileList | null) {
    setBusy(true);
    setError(null);
    setNote(null);
    try {
      if (choice === "bank") {
        if (!files?.length) throw new ApiError(0, "Choose a CSV file first");
        const result = await api.uploadBankCsv(workspaceId, files[0]);
        setNote(`${result.canonical_written} transactions imported.`);
      } else if (choice === "contracts") {
        if (!files?.length) throw new ApiError(0, "Choose at least one PDF");
        const result = await api.uploadContracts(workspaceId, Array.from(files));
        setNote(`${result.accepted.length} contracts vaulted.`);
      } else {
        const run = await api.runIngestion(
          workspaceId,
          choice === "template" || choice === "generated",
          choice === "generated" ? seed : undefined,
        );
        setNote(`${run.total_canonical} records collected across ${Object.keys(run.sources).length} sources.`);
      }
      onLoaded();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load the evidence");
    } finally {
      setBusy(false);
    }
  }

  const needsFile = choice === "bank" || choice === "contracts";

  return (
    <div className="w-[352px] overflow-hidden rounded-[10px] border border-line bg-paper shadow-[0_14px_40px_-8px_rgba(8,17,31,0.24)]">
      <header className="flex items-center justify-between border-b border-line px-3.5 py-2.5">
        <h3 className="text-[13px] font-semibold text-ink">Choose evidence source</h3>
        <button
          onClick={onClose}
          aria-label="Close"
          className="rounded p-1 text-ink-3 hover:bg-slate-soft hover:text-ink"
        >
          <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden>
            <path d="m4 4 8 8M12 4l-8 8" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
          </svg>
        </button>
      </header>

      <div className="max-h-[290px] overflow-y-auto scroll-thin py-1">
        {options.map((option) => (
          <button
            key={option.id}
            onClick={() => {
              setChoice(option.id);
              setError(null);
              setNote(null);
            }}
            className={`flex w-full items-start gap-2.5 px-3.5 py-2 text-left transition-colors ${
              choice === option.id ? "bg-cobalt-soft" : "hover:bg-slate-soft"
            }`}
          >
            <span
              className={`mt-[3px] grid h-[15px] w-[15px] shrink-0 place-items-center rounded-full border-[1.5px] ${
                choice === option.id ? "border-cobalt" : "border-line"
              }`}
            >
              {choice === option.id && <span className="h-[7px] w-[7px] rounded-full bg-cobalt" />}
            </span>
            <span className="min-w-0 flex-1">
              <span className="flex items-baseline justify-between gap-2">
                <span className="text-[12.5px] font-medium text-ink">{option.name}</span>
                {option.badge && (
                  <span
                    className={`shrink-0 text-[10.5px] ${
                      option.badgeTone === "ready" ? "text-emerald" : "text-ink-3"
                    }`}
                  >
                    {option.badge}
                  </span>
                )}
              </span>
              <span className="mt-0.5 block text-[11.5px] leading-snug text-ink-2">
                {option.detail}
              </span>
            </span>
          </button>
        ))}
      </div>

      <div className="space-y-2.5 border-t border-line px-3.5 py-3">
        {choice === "generated" && (
          <label className="block">
            <span className="mb-1 block text-[11.5px] text-ink-2">Seed</span>
            <div className="flex gap-1.5">
              <input
                value={seed}
                onChange={(e) => setSeed(e.target.value)}
                className="h-8 min-w-0 flex-1 rounded-[6px] border border-line px-2.5 font-mono text-[12px] focus:border-cobalt focus:outline-none"
              />
              <Button
                size="sm"
                onClick={() => setSeed(SEEDS[Math.floor(Math.random() * SEEDS.length)])}
              >
                Shuffle
              </Button>
            </div>
          </label>
        )}

        {choice === "live" && (
          <Banner tone="warn">
            None of the four is a bank, so receipts stop at &ldquo;verified by the
            processor&rdquo; until you upload the bank statement covering this period.
          </Banner>
        )}

        {error && <Banner tone="error">{error}</Banner>}
        {note && <Banner tone="success">{note}</Banner>}

        {needsFile ? (
          <label className="block">
            <input
              type="file"
              accept={choice === "bank" ? ".csv,text/csv" : "application/pdf"}
              multiple={choice === "contracts"}
              disabled={busy}
              onChange={(e) => void load(e.target.files)}
              className="block w-full text-[12px] text-ink-2 file:mr-2 file:h-8 file:cursor-pointer file:rounded-[6px] file:border-0 file:bg-cobalt file:px-3 file:text-[12px] file:font-medium file:text-white hover:file:bg-cobalt/90"
            />
          </label>
        ) : (
          <Button
            variant="primary"
            className="w-full"
            disabled={busy}
            onClick={() => void load()}
            icon={busy ? <Spinner /> : undefined}
          >
            {busy ? "Loading evidence…" : "Load evidence"}
          </Button>
        )}
      </div>
    </div>
  );
}
