# RevenueProof

**Does the revenue a startup claims actually exist?**

**Live:** https://revenueproof.vercel.app · **Code:** https://github.com/mayank-badalia/revenueproof

---

## The problem, in one paragraph

An investor is handed a spreadsheet that says "we did ₹1.5 crore last year." Checking
that by hand means opening five different systems — the accounting software, the
payment gateway, the CRM, a folder of contract PDFs, a bank statement — and tracing
every rupee across all of them. A junior analyst spends two weeks on it. Most of the
time nobody does it properly at all.

RevenueProof does that tracing automatically, and shows its work.

## What it actually does

It builds the evidence chain behind every rupee:

```
Customer → Contract → Invoice → Payment → Bank Receipt → Refund
```

Then it answers one question per amount: **is there a complete chain behind this, or
isn't there?** Money with a full chain is proven. Money missing a link is reported
along with exactly which link is missing.

Click any figure in the app and you can walk the whole chain, down to the page and
line of the contract that produced it.

> **It does not accuse anyone of anything.** A missing invoice usually means a missing
> invoice, not fraud. The app shows what the evidence supports and what it doesn't,
> and says plainly that it is not investment advice and does not certify revenue.

---

## The result on the demo dataset

One run takes about **100 seconds** end to end.

| | |
|---|---|
| Claimed by the founder | ₹1,50,00,000 |
| **Proven and published** | **₹1,12,62,000 — 75.1%** |
| Held back pending a human decision | ₹24,19,000 |
| After a reviewer clears those | **91.2%** |

That middle row is the honest part and worth understanding. ₹24,19,000 is money the
system *did* trace a full chain for, but something else about it is unresolved — an
open high-severity warning touches it. Rather than quietly counting it, the app holds
it back and tells the reviewer exactly which question to answer. Answering it releases
the money.

**A tool that reports a number it cannot defend is worse than useless in diligence.**
So the headline only ever contains what survived every check.

### The tricky cases it is built to catch

The demo dataset deliberately plants all of these. The detectors find them even on
randomly generated companies they have never seen before:

- **One customer spelled four ways** across four systems — correctly merged into one
- **Two companies with nearly identical names** — correctly *not* merged, because their
  tax IDs conflict
- **A one-time setup fee sold as an annual subscription** — split, so ARR isn't inflated
- **A payment refunded a week later** — not counted as revenue
- **One payment agent settling for two different customers** — both traced
- **Money that arrives and leaves again** (a circular flow) — the round trip closed
- **Cash with no invoice or contract behind it** — reported, never counted

---

## How the pipeline works

Seven stages. Each one refuses to run if the stage it depends on hasn't run yet, so
you can never get a number built on missing input.

**1 · Collect the evidence**
Pulls from Razorpay, Zoho Books, HubSpot and Google Drive, plus a bank statement CSV.
Every original record is stored untouched with a SHA-256 hash, so you can always prove
what the source actually said. *231 records across 5 sources.*

**2 · Work out who the customers are**
"NSTAR TECH PVT LTD", "Northstar Technologies" and "northstar.io" may be one company —
or two. The app matches on names, domains, tax IDs and email, and refuses to merge when
identifiers conflict. *37 customer records from two systems resolved into 24 real
customers — 103 matches accepted, 75 rejected, 10 sent to a human.*

**3 · Read the contracts**
Opens each PDF and pulls out what's recurring versus one-time. Every number it extracts
carries a **page citation that gets re-verified** against the actual text — if the
citation doesn't check out, the number is not used. *12 of 14 contracts read.*

**4 · Reconcile the cash**
Matches invoices to payments to bank credits, and subtracts refunds. This is a
constraint-solving problem, not fuzzy matching — it uses Google OR-Tools, and the
totals are verified to conserve to the last paisa. *53 allocations, conservation
verified.*

**5 · Classify every amount**
Each amount lands in one of eight states: verified recurring, verified one-time,
contracted but unbilled, invoiced but unpaid, refunded, cash without support,
unsupported claim, or needs-a-human. *64 items.*

**6 · Look for what's odd**
Three independent detectors — hand-written rules, an explainable ML model, and a graph
search for related parties and circular money — run separately and then combine.
*27 indicators across 10 rules, 6 of them high severity.*

**7 · Argue against everything, then publish**
A second AI model, from a **different family than the one that proposed the answer**,
re-reads the original evidence and tries to knock each classification down. Anything it
can't settle goes to a human. *41 approved, 21 disputed, 2 needing more evidence.*

---

## Five design decisions worth knowing

**Money is never a floating-point number.** Integer paise everywhere, including in the
frontend, which only displays strings the backend formatted and does no arithmetic of
its own. Split ₹1.00 three ways and it still adds back to exactly ₹1.00.

**The AI cannot move a number.** All arithmetic, dates, currency handling, refund
subtraction and permissions are plain Python. The model reads contract prose and argues
about classifications — that's it. Run it three times on identical evidence and the
figures come out identical every time.

**The critic can object but cannot silently veto.** It reads the original documents
rather than the first model's summary, and it can only ever *weaken* a claim. What it
can't do is delete a figure that passed every arithmetic check — an AI verdict that
changes between runs must never move a published number. Its objection is recorded and
put in front of a person instead.

**Real books are never mixed with demo data.** If a connector pulls from a genuine
account, the app refuses to seed its sample bank statement alongside it, and says so.
Manufacturing evidence in a tool built to verify evidence is the worst thing it could
do.

**One number, one definition.** The on-screen figure and the downloadable report import
the same function. A due-diligence tool that gives two answers to one question has
failed at the only thing it does.

---

## Try it yourself

### Fastest path

```bash
./scripts/serve-demo.sh
```

Starts the databases, the API and a public tunnel in one command.

### Step by step

```bash
# 1. Databases (macOS: colima start --cpu 4 --memory 8 first)
cd infra && docker compose up -d          # postgres, redis, neo4j, chromadb

# 2. Backend
cd backend
uv venv --python 3.13 && uv pip install -e ".[dev]"
cp ../.env.example ../.env                # add CEREBRAS_API_KEY for the AI stages
.venv/bin/python -m uvicorn app.main:app --port 8000

# 3. Frontend
cd frontend && npm install && npm run dev
```

Open http://localhost:3000, register, create a workspace, pick **Demonstration data**,
and press **Run everything**.

**Without an AI key, every financial figure still computes.** The money engine,
reconciliation, classification and rule-based detectors are pure Python. Only contract
reading and the critic's written reasoning need a model.

---

## Checking that it works

```bash
cd backend
.venv/bin/python -m pytest -q            # 535 tests
.venv/bin/python scripts/verify_all.py   # 93 end-to-end checks
```

`verify_all.py` is the interesting one. It builds a workspace from scratch **through
the real HTTP API**, runs every stage, downloads the report and the dataset, and opens
them to read what's inside. It asserts the things a reviewer would otherwise check by
hand:

- re-running ingestion creates no duplicate records
- the false-merge trap was not fallen for
- every planted adversarial case was caught
- nothing that fails an arithmetic check gets published
- the evidence chain actually reaches a bank credit
- the audit log's hash chain verifies
- the downloaded bank statement's running balance reconciles across all 62 rows
- **the on-screen figure and the downloaded report quote the same number**

Where a check genuinely can't be made, it prints SKIP rather than passing quietly.

---

## What it's built with

| Layer | Choice |
|---|---|
| Backend | FastAPI, Python 3.13, async throughout |
| Database | PostgreSQL 17, row-level security on all 22 tenant tables |
| Graph | Neo4j — related parties, circular money flows |
| Matching | OR-Tools CP-SAT solver over integer paise |
| ML | scikit-learn Isolation Forest, gated on its own measured precision |
| AI | Cerebras `gemma-4-31b` proposes, `gpt-oss-120b` criticises |
| Frontend | Next.js 16, React 19, TypeScript, Tailwind 4 |

---

## Security

- **Tenant isolation** is enforced by PostgreSQL row-level security, default-deny and
  `FORCE`d so even the table owner can't bypass it, plus application-level checks. A
  non-member gets a 404, not a 403 — so the app never reveals that a workspace exists.
- **Provider tokens** are encrypted at rest and never placed in an AI prompt.
- **The audit log** is append-only and SHA-256 hash-chained, with a live integrity
  verdict shown in the UI.
- **Contract text is treated as hostile.** It was written by the company under review,
  so it's wrapped as untrusted data before any model sees it.
- **Webhooks are hints, not data.** Signatures are verified, deliveries deduplicated,
  and the real record is then re-fetched from the provider — so a replayed or forged
  webhook cannot change stored evidence.

`.env` is gitignored and no credential is committed. Rotate any key that has ever been
shared in a chat or a screen recording.

---

## Deployment

The frontend runs on Vercel. The backend needs a container, and the reason is
arithmetic rather than preference:

- 619 MB of Python dependencies against a 250 MB serverless function limit
- four stateful services (Postgres, Redis, Neo4j, ChromaDB)
- a WebSocket for the live processing trace
- runs lasting 47–113 seconds against a 60-second function limit

`backend/Dockerfile` packages it — measured at 140 MB idle and 410 MB peak, so it fits
a 512 MB instance. It runs on any container host, or locally behind a tunnel:

```bash
cd frontend
echo "https://<tunnel-url>" | vercel env add NEXT_PUBLIC_API_BASE production
vercel --prod    # the API URL is compiled into the bundle, so redeploy after changing it
```

Set `EXTRA_ALLOWED_ORIGINS` in `.env` to your Vercel URL, or the browser will block
every request.
