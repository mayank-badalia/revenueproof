# RevenueProof

### Does the revenue a startup claims actually exist?

**Live demo:** https://revenueproof.vercel.app · **Code:** https://github.com/mayank-badalia/revenueproof

RevenueProof takes a founder's revenue claim and tries to prove it — or fails honestly
and tells you exactly which piece of evidence is missing.

---

## Problem Statement

**An investor is handed a spreadsheet that says "we did ₹1.5 crore last year." How do
they know it's true?**

To actually check it, someone has to open five different systems and trace every rupee
across all of them:

| Where the truth is scattered | What it holds |
|---|---|
| Accounting software (Zoho Books) | invoices, credit notes, customers |
| Payment gateway (Razorpay) | payments, refunds, disputes |
| CRM (HubSpot) | company records, contacts |
| A Drive folder | contract PDFs, unread |
| A bank statement CSV | what money actually arrived |

The same customer is spelled four different ways across those five systems. A one-time
setup fee sits in the books looking exactly like an annual subscription. A payment that
was refunded three weeks later still shows as revenue. An invoice was raised but never
paid, and nothing in the spreadsheet says so.

**This takes a junior analyst two weeks. Most of the time, nobody does it properly at
all** — and the number goes into a term sheet unchecked.

---

## What It Solves

RevenueProof does that tracing automatically, in about **100 seconds**, and shows its
work. Concretely, it replaces:

- **Two weeks of manual cross-referencing** → one run, fully automated
- **"Trust me, it's ₹1.5 crore"** → a figure with a clickable chain behind every rupee
- **A single unexplained total** → every amount sorted into one of eight states, each
  with the rule that put it there
- **Silent gaps** → each missing invoice, unpaid bill and refunded payment named and
  quantified
- **An answer you can't check** → a downloadable report where every number cites its
  source document, page and line

> **It never accuses anyone of anything.** A missing invoice usually means a missing
> invoice, not fraud. RevenueProof reports what the evidence supports and what it
> doesn't. It explicitly does **not** certify revenue and is **not** investment advice.

---

## The Solution

Every rupee of real revenue leaves the same trail:

```
Customer → Contract → Invoice → Payment → Bank Receipt → Refund
```

RevenueProof rebuilds that chain for every amount and asks one question:

> **Is the chain complete, or is it broken?**

Money with a complete chain is **proven**. Money with a broken chain is **reported**,
along with exactly which link is missing and what you'd have to upload to close it.

Click any figure in the app and you can walk its chain end to end — down to the page
and line of the contract that produced it.

### Two ideas that make it trustworthy

**1 · The AI cannot move a number.** All arithmetic, dates, currency, refund
subtraction and permissions are plain Python. The AI only reads contract prose and
argues about classifications. Run it three times on the same evidence and the figures
come out identical every time.

**2 · A second AI argues against the first.** Before anything is published, a critic
model from a *different family* re-reads the original documents and tries to knock each
classification down. Anything it can't settle goes to a human instead of being counted.

---

## Features

Eight features, each one a full stage of the product.

### 1 · Evidence Collection
Connects to Razorpay, Zoho Books, HubSpot and Google Drive, plus bank statement CSV and
contract PDF upload. Every original record is stored untouched with a SHA-256 hash, so
you can always prove what the source actually said. Webhooks are treated as *hints*:
the signature is verified, the delivery deduplicated, and then the real record is
re-fetched from the provider — so a replayed or forged webhook cannot corrupt evidence.

### 2 · Customer Identity Resolution
Decides which records across five systems are the same company. Matches on names,
domains, tax IDs and email addresses — and **refuses to merge when identifiers
conflict**, so two genuinely different companies with near-identical names stay
separate. Uncertain matches go to a human rather than being guessed.

### 3 · Contract Intelligence
Opens each PDF (digital text or OCR), finds the clauses that matter, and separates
recurring subscription value from one-time fees. **Every extracted number carries a
page citation that is re-verified against the actual text** — if the citation doesn't
check out, the number is not used.

### 4 · Cash Reconciliation
Matches invoices to payments to bank credits and subtracts refunds. This is a
constraint-solving problem rather than fuzzy matching, so it uses Google OR-Tools
CP-SAT over integer paise, and the totals are verified to conserve exactly.

### 5 · Revenue Verification
Classifies every amount into one of **eight states** and sets it beside the claim:

`Verified Recurring` · `Verified One-Time` · `Contracted but Unbilled` ·
`Invoiced but Unpaid` · `Refunded or Reversed` · `Cash Without Support` ·
`Unsupported Claim` · `Needs Human Review`

### 6 · Anomaly Detection
Three independent detectors — hand-written rules, an explainable ML model
(Isolation Forest) and a graph search for related parties and circular money — run
separately and are then joined. The ML model is **gated on its own measured
precision**: if it isn't performing, it doesn't run.

### 7 · Adversarial Critic & Human Review
A second model argues against every material classification. It can only ever *weaken*
a claim, never strengthen one. Everything unresolved lands in a review queue where
equivalent questions are collapsed into a single decision — answer once, resolve many,
each keeping its own audit entry.

### 8 · Diligence Room
The published position, the history of how it moved, and the evidence chain behind
every figure. Publish a version to freeze it, then re-run later and see precisely what
changed and why.

---

## Results on the demo dataset

One full run: **~100 seconds**.

| | |
|---|---|
| Claimed by the founder | ₹1,50,00,000 |
| **Proven and published** | **₹1,12,62,000 — 75.1%** |
| Held back pending a human decision | ₹24,19,000 |
| After a reviewer clears those | **91.2%** |

That middle row is the honest part. ₹24,19,000 is money the system *did* trace a
complete chain for — but an unresolved high-severity warning touches it. Rather than
quietly counting it, the app holds it back and names the exact question that releases
it.

**A tool that reports a number it cannot defend is worse than useless in diligence.**
The headline only ever contains what survived every check.

### The tricky cases it catches

The demo dataset plants all of these deliberately. The detectors find them even on
randomly generated companies they have never seen:

| Planted case | Correct behaviour |
|---|---|
| One customer spelled four ways across four systems | merged into one |
| Two companies with near-identical names | **not** merged — tax IDs conflict |
| One-time setup fee sold as an annual subscription | split, so ARR isn't inflated |
| A payment refunded a week later | not counted as revenue |
| One payment agent settling for two customers | both traced correctly |
| Money that arrives and leaves again | round trip closed |
| Cash with no invoice or contract behind it | reported, never counted |

---

## How the pipeline runs

Seven stages. **Each refuses to run if the stage it depends on hasn't run**, so you can
never get a number built on missing input.

| # | Stage | What it does | Measured result |
|---|---|---|---|
| 1 | Collect evidence | pull from 5 sources, hash everything | 231 records |
| 2 | Resolve identities | who is actually the same customer | 37 records → 24 customers; 103 matches accepted, 75 rejected, 10 to a human |
| 3 | Read contracts | recurring vs one-time, with verified citations | 12 of 14 read |
| 4 | Reconcile cash | invoices → payments → bank, minus refunds | 53 allocations, conservation verified |
| 5 | Verify revenue | classify into 8 states against the claim | 64 items |
| 6 | Scan anomalies | rules ∥ ML ∥ graph, then joined | 27 indicators, 10 rules, 6 high severity |
| 7 | Critic & publish | argue against everything, then freeze | 41 approved, 21 disputed, 2 need evidence |

---

## Design decisions worth knowing

**Money is never a floating-point number.** Integer paise everywhere, including the
frontend, which only displays strings the backend formatted and does no arithmetic of
its own. Split ₹1.00 three ways and it still adds back to exactly ₹1.00.

**The critic can object but cannot silently veto.** It reads original documents rather
than the first model's summary. What it can't do is delete a figure that passed every
arithmetic check — an AI verdict that changes between runs must never move a published
number. Its objection is recorded and put in front of a person instead.

**Real books are never mixed with demo data.** If a connector pulls from a genuine
account, the app refuses to seed its sample bank statement alongside it, and says so.
Manufacturing evidence inside a tool built to verify evidence is the worst thing it
could do.

**One number, one definition.** The on-screen figure and the downloadable report import
the same function. A due-diligence tool that gives two answers to one question has
failed at the only thing it does.

**Uncertainty always goes to a human.** `HUMAN_REVIEW` is a real state with a real
queue behind it, not a label that quietly drops the amount.

---

## Try it yourself

### Fastest path

```bash
./scripts/serve-demo.sh
```

Starts the databases, the API and a public tunnel in one command.

### Step by step

```bash
# 1. Databases  (macOS: colima start --cpu 4 --memory 8 first)
cd infra && docker compose up -d          # postgres, redis, neo4j, chromadb

# 2. Backend
cd backend
uv venv --python 3.13 && uv pip install -e ".[dev]"
cp ../.env.example ../.env                # add CEREBRAS_API_KEY for the AI stages
.venv/bin/python -m uvicorn app.main:app --port 8000

# 3. Frontend
cd frontend && npm install && npm run dev
```

Open http://localhost:3000 → register → create a workspace → pick **Demonstration
data** → press **Run everything**.

**Without an AI key, every financial figure still computes.** The money engine,
reconciliation, classification and rule-based detectors are pure Python. Only contract
reading and the critic's written reasoning need a model.

---

## Verifying it works

```bash
cd backend
.venv/bin/python -m pytest -q            # 535 tests
.venv/bin/python scripts/verify_all.py   # 93 end-to-end checks
```

`verify_all.py` is the interesting one. It builds a workspace from scratch **through
the real HTTP API**, runs every stage, downloads the report and dataset, and opens them
to read what's inside. It asserts what a reviewer would otherwise check by hand:

- re-running ingestion creates no duplicate records
- the false-merge trap was not fallen for
- every planted adversarial case was caught
- nothing failing an arithmetic check gets published
- the evidence chain actually reaches a bank credit
- the audit log's hash chain verifies
- the downloaded statement's running balance reconciles across all 62 rows
- **the on-screen figure and the downloaded report quote the same number**

Where a check genuinely cannot be made, it prints SKIP rather than passing quietly.

---

## Tech stack

| Layer | Choice |
|---|---|
| Backend | FastAPI, Python 3.13, async throughout |
| Database | PostgreSQL 17, row-level security on all 22 tenant tables |
| Graph | Neo4j — related parties, circular money flows |
| Matching | OR-Tools CP-SAT solver over integer paise |
| ML | scikit-learn Isolation Forest, gated on measured precision |
| AI | Cerebras `gemma-4-31b` proposes · `gpt-oss-120b` criticises |
| Frontend | Next.js 16, React 19, TypeScript, Tailwind 4 |

---

## Security

- **Tenant isolation** via PostgreSQL row-level security — default-deny and `FORCE`d so
  even the table owner can't bypass it, plus application-level checks. A non-member gets
  a **404, not a 403**, so the app never reveals that a workspace exists.
- **Provider tokens** are encrypted at rest and never placed in an AI prompt.
- **The audit log** is append-only and SHA-256 hash-chained, with a live integrity
  verdict in the UI.
- **Contract text is treated as hostile** — it was written by the company under review,
  so it's wrapped as untrusted data before any model sees it.
- **Webhooks are hints, not data** — verified, deduplicated, then the real record is
  re-fetched from the provider.

`.env` is gitignored and no credential is committed. Rotate any key that has ever been
shared in a chat or a screen recording.

---

## Deployment

The frontend runs on Vercel. The backend needs a **container**, and the reason is
arithmetic rather than preference:

- 619 MB of Python dependencies vs a 250 MB serverless function limit
- four stateful services (Postgres, Redis, Neo4j, ChromaDB)
- a WebSocket for the live processing trace
- runs lasting 47–113 seconds vs a 60-second function limit

`backend/Dockerfile` packages it — measured at 140 MB idle, 410 MB peak, so it fits a
512 MB instance. It runs on any container host, or locally behind a tunnel:

```bash
cd frontend
echo "https://<tunnel-url>" | vercel env add NEXT_PUBLIC_API_BASE production
vercel --prod    # the API URL is compiled into the bundle, so redeploy after changing it
```

Set `EXTRA_ALLOWED_ORIGINS` in `.env` to your Vercel URL, or the browser blocks every
request.
