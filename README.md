# RevenueProof

### Does the revenue a startup claims actually exist?

**Live demo:** https://revenueproof.vercel.app · **Code:** https://github.com/mayank-badalia/revenueproof

---

## In simple words

1. A startup says it earned **₹1.5 crore** last year.
2. If that's true, the money left a trail — a contract, an invoice, a payment, and cash landing in a bank account.
3. RevenueProof opens the company's accounting software, payment gateway, CRM, contract PDFs and bank statement, and **rebuilds that trail for every single rupee**.
4. Money with a complete trail is **proven**. Money with a broken trail is **reported**, naming exactly which piece is missing.
5. You end up with a number you can click into and check yourself — instead of one you simply have to trust.

---

## Problem Statement

**An investor is handed a spreadsheet that says "we did ₹1.5 crore last year." How do
they know it's true?**

To actually check it, someone has to open five different systems and trace every rupee
across all of them:

| Where the truth is scattered | What it holds |
|---|---|
| Accounting software (Zoho Books) | invoices, credit notes, customers |
| Payment gateway (Razorpay) | payments, refunds, disputes, settlements |
| CRM (HubSpot) | company records, contacts |
| A Google Drive folder | contract PDFs, usually unread |
| A bank statement CSV | what money actually arrived |

And the systems disagree with each other in ways that are easy to miss:

- **The same customer is spelled four different ways.** "NSTAR TECH PVT LTD" in the
  bank statement, "Northstar Technologies Pvt. Ltd." in the invoice, "Northstar Tech"
  in the CRM, "northstar.io" as a domain. Count them separately and your customer
  concentration looks far safer than it is.
- **A one-time fee is sitting inside a subscription number.** An invoice reading
  "Annual subscription — implementation and migration programme" for ₹18,00,000 looks
  like ₹18,00,000 of recurring revenue. The contract says ₹15,00,000 of it is a
  one-off implementation fee. That difference is pure ARR inflation.
- **A payment was refunded three weeks later** and still shows as revenue.
- **An invoice was raised but never paid**, and nothing in the spreadsheet says so.
- **Cash arrived with no invoice behind it** — which may be perfectly innocent, but
  nobody can tell you what it was for.

**Checking this by hand takes a junior analyst two weeks. Most of the time nobody does
it properly at all** — and the number goes into a term sheet unverified.

---

## What It Solves

RevenueProof does that tracing automatically in about **100 seconds**, and shows its
work. Concretely, it replaces:

| Before | After |
|---|---|
| Two weeks of manual cross-referencing | One run, fully automated |
| "Trust me, it's ₹1.5 crore" | A figure with a clickable chain behind every rupee |
| One unexplained total | Every amount sorted into one of eight states, each with the rule that put it there |
| Silent gaps | Each missing invoice, unpaid bill and refunded payment named and quantified |
| An answer you cannot check | A downloadable report where every number cites its source document, page and line |

> **It never accuses anyone of anything.** A missing invoice usually means a missing
> invoice, not fraud. RevenueProof reports what the evidence supports and what it
> doesn't. Findings are always worded as *"anomaly indicator, requires review"* —
> never as an allegation. It explicitly does **not** certify revenue and is **not**
> investment advice.

---

## The Solution

Every rupee of genuine revenue leaves the same trail:

```
Customer → Contract → Invoice → Payment → Bank Receipt → Refund
```

RevenueProof rebuilds that chain for every amount and asks one question:

> **Is the chain complete, or is it broken?**

Money with a complete chain is **proven**. Money with a broken chain is **reported**,
along with exactly which link is missing and what you would have to upload to close it.

Click any figure in the app and you can walk its chain end to end — down to the page
and line of the contract that produced it.

### A worked example

The demo dataset contains this invoice from Quantum Retail:

```
INV-2026-032   "Annual subscription — implementation and migration programme"
               ₹18,00,000
```

Read naively, that is ₹18,00,000 of annual recurring revenue. RevenueProof opens the
underlying contract, finds the clause that governs it, and splits it:

```
₹3,00,000   recurring    ← the actual annual subscription
₹15,00,000  one-time     ← the implementation fee, explicitly non-recurring
```

**That ₹15,00,000 is exactly what would otherwise inflate ARR by 6×** on this one
customer. Every number in that split carries a page citation, and the citation is
re-verified against the actual PDF text — if it doesn't check out, the number is
thrown away rather than used.

### Three ideas that make the output trustworthy

**1 · The AI cannot move a number.** All arithmetic, dates, currency handling, refund
subtraction and permissions are plain Python. The AI only reads contract prose and
argues about classifications. Run it three times on identical evidence and the figures
come out identical every time. *Without an AI key at all, every financial figure still
computes.*

**2 · A second AI argues against the first.** Before anything is published, a critic
model **from a different family** re-reads the original documents and tries to knock
each classification down. Two models from the same family agreeing is not independent
verification — it is the same prior, twice.

**3 · Uncertainty goes to a human, never to a guess.** "Needs human review" is a real
state with a real queue behind it, not a label that quietly drops the amount.

---

## Features

Eight features, each a full stage of the product.

### 1 · Evidence Collection & Provenance Vault
Connects to Razorpay, Zoho Books, HubSpot and Google Drive, plus bank statement CSV
and contract PDF upload. **Every original record is stored untouched with a SHA-256
hash**, so you can always prove what the source actually said — canonical records are
re-derivable from it, meaning improved parsing never rewrites history.

Bad rows are *quarantined and shown to you* rather than silently dropped. Uploads are
checked on magic bytes rather than file extension, because the extension is chosen by
whoever sends the file.

**Webhooks are treated as hints, not data.** The signature is verified against the raw
body, the delivery is deduplicated by event ID, and then the real record is re-fetched
from the provider — so a replayed or forged webhook cannot corrupt stored evidence.

### 2 · Cross-System Customer Identity
Decides which records across five systems are the same company. This job is
**asymmetric, and both halves matter**:

- Merge four spellings of Northstar into one customer, or concentration looks safer
  than it is.
- Keep "Blue Harbor Analytics" and "Blue Harbour Logistics" *apart*, because they are
  genuinely different companies.

Matches on names, domains, tax IDs (including GSTIN→PAN extraction, since two GSTINs
sharing a PAN are one legal entity in different states) and email. **It refuses to
merge when identifiers conflict** — and uncertain matches go to a human rather than
being guessed.

### 3 · Contract Revenue Intelligence
Opens each PDF — native text where possible, OCR when the file is image-only — finds
the clauses that matter, and separates recurring subscription value from one-time fees
and future-period value.

**Every extracted number carries a page citation that is re-verified against the actual
text.** If the quote cannot be found where the model said it was, the number is
discarded rather than used. An unread contract shows as *"not read"*, never as ₹0 —
because a contract worth zero and a contract nobody opened are completely different
facts.

### 4 · Contract-to-Cash Reconciliation
Matches invoices to payments to bank credits and subtracts refunds. Fully
deterministic — **zero AI calls**.

This is a constraint-solving problem rather than fuzzy matching, so it uses Google
OR-Tools CP-SAT over integer paise, with hard constraints that make double-counting
*structurally impossible* rather than something a later check has to catch. Bank
credits are matched **net of processor fee and tax**, because "captured" and "settled"
are different events. Totals are verified to conserve exactly.

It handles the awkward real cases: one bank credit settling four invoices, three
instalments against one invoice, and a partial refund of a combined payment split
proportionally across every invoice it touched.

### 5 · Revenue Truth & ARR Verification
Classifies every amount into one of **eight mutually exclusive states** and sets it
beside the claim:

| State | What it means |
|---|---|
| **Verified Recurring** | Paid, kept, and a contract says it repeats |
| **Verified One-Time** | Paid and kept, but a one-off |
| **Contracted but Unbilled** | Under contract, never invoiced — contracted value is not cash |
| **Invoiced but Unpaid** | An invoice is a claim on cash, not proof of it |
| **Refunded or Reversed** | The money came and went again |
| **Cash Without Support** | Money arrived with no invoice or contract explaining it |
| **Unsupported Claim** | Claimed, with nothing behind it |
| **Needs Human Review** | A contradiction only a person can settle |

**Rule ordering is the design.** Refunds are checked *before* any verification rule,
because a paid-then-refunded item has complete-looking evidence and is exactly what a
company is most likely to still be counting.

### 6 · Anomaly & Manipulation Detection
Three independent detectors run separately and are then joined:

- **Hand-written rules** with stated thresholds and stated justifications
- **An explainable ML model** (Isolation Forest), validated so no future period judges
  a past one
- **A graph search** for related parties and circular money flows

The ML model is **gated on its own measured precision** — if it isn't performing, it
doesn't run, and its findings can never open a review item on their own. Customer
concentration is reported as top-1, top-5 and HHI, always stating what it was divided
by.

### 7 · Adversarial Critic & Human Resolution
The maker-checker. Feature 5 proposes; the critic argues the other side before anything
is published.

**Deterministic checks run first and cannot be overruled** — recognising more than a
payment retained, a refund not applied, an unresolved customer, a void invoice, an
unverified citation. The critic reads *original evidence*, not the proposer's summary,
because reviewing a summary is how two agents agree about a mistake. **It can only ever
weaken a claim**, never strengthen one.

What it cannot do is silently veto. An AI verdict that changes between runs must never
move a published number, so its objection is recorded, shown on the item, and routed to
a person instead.

The review queue **collapses equivalent questions into one decision**. Asking "is Blue
Harbor the same as Blue Harbour?" seventeen times is how people learn to click without
reading. Answer once, resolve many — each record keeping its own audit entry. A
decision cannot be recorded without a written reason.

### 8 · Living Evidence Graph & Diligence Room
The published position, the history of how it moved, and the chain behind every figure.

Publish a version to freeze it; re-run later and see precisely what changed, in which
direction and by how much — computed in code, never narrated by a model. **A break in a
chain is the finding, not an error**, so it is reported rather than quietly producing a
shorter chain. Withheld amounts sit beside published ones carrying the reason they were
withheld, because a gap you can see is worth more than a total you cannot check.

---

## Results on the demo dataset

One full run: **~100 seconds**.

| | |
|---|---|
| Claimed by the founder | ₹1,50,00,000 |
| **Proven and published** | **₹1,12,62,000 — 75.1%** |
| Held back pending a human decision | ₹24,19,000 |
| After a reviewer clears those | **91.2%** |

That middle row is the honest part, and worth understanding. ₹24,19,000 is money the
system *did* trace a complete chain for — but an unresolved high-severity indicator
touches it. Rather than quietly counting it, the app holds it back and names the exact
question that releases it. Answer the indicator, and the money publishes.

**A tool that reports a number it cannot defend is worse than useless in diligence.**
The headline only ever contains what survived every check.

### The tricky cases it catches

The demo dataset plants all of these deliberately. The detectors find them **even on
randomly generated companies they have never seen** — which is the real test of whether
the product works or merely works on the demo.

| Planted case | Correct behaviour |
|---|---|
| One customer spelled four ways across four systems | merged into one |
| Two companies with near-identical names | **not** merged — tax IDs conflict |
| One-time setup fee sold as an annual subscription | split, so ARR isn't inflated |
| A payment refunded a week later | not counted as revenue |
| One payment agent settling for two customers | both traced correctly |
| Money that arrives and leaves again | round trip closed |
| A parent company paying its subsidiary's invoice | related party flagged |
| Cash with no invoice or contract behind it | reported, never counted |
| A contract whose value falls in a future year | excluded from this period |

---

## How the pipeline runs

Seven stages. **Each refuses to run if the stage it depends on hasn't run**, so you can
never get a number built on missing input. Running an empty workspace is refused
outright — every stage would "succeed" over nothing and report the claim proven at 0%,
which is indistinguishable from a claim that was checked and failed.

| # | Stage | What it does | Measured result |
|---|---|---|---|
| 1 | **Collect evidence** | pull from 5 sources, hash everything | 231 records |
| 2 | **Resolve identities** | who is actually the same customer | 37 records → 24 customers; 103 matches accepted, 75 rejected, 10 to a human |
| 3 | **Read contracts** | recurring vs one-time, with verified citations | 12 of 14 read |
| 4 | **Reconcile cash** | invoices → payments → bank, minus refunds | 53 allocations, conservation verified |
| 5 | **Verify revenue** | classify into 8 states against the claim | 64 items |
| 6 | **Scan anomalies** | rules ∥ ML ∥ graph, then joined | 27 indicators, 10 rules, 6 high severity |
| 7 | **Critic & publish** | argue against everything, then freeze | 41 approved, 21 disputed, 2 need evidence |

---

## Design decisions worth knowing

**Money is never a floating-point number.** Integer paise everywhere, including in the
frontend, which only displays strings the backend formatted and does no arithmetic of
its own. Split ₹1.00 three ways and it still adds back to exactly ₹1.00. Rupees are
grouped Indian-style (`1,00,00,000`) everywhere, because a figure written `10,000,000`
is read as one crore by nobody in this audience.

**The critic can object but cannot silently veto.** Measured over three runs on
byte-identical evidence, the classifier produced the same figure every time while the
critic's published total swung by 52% — because models return different verdicts on the
same input even at temperature 0. A figure that moves while the evidence does not is
exactly what this product promises never to produce. Publication is now decided by the
deterministic half, and the swing dropped to 1%.

**Real books are never mixed with demo data.** If a connector pulls from a genuine
account, the app refuses to seed its sample bank statement alongside it, and says so.
Manufacturing evidence inside a tool built to verify evidence is the worst thing it
could possibly do.

**One number, one definition.** The on-screen figure and the downloadable report import
the same function. A due-diligence tool that gives two answers to one question has
failed at the only thing it does.

**Raw evidence is immutable and kept separate from canonical records.** Improving how
data is parsed never rewrites what a provider originally said.

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

**Ports:** API 8000 · UI 3000 · Postgres 55432 · Redis 56379 · Neo4j 57687
(browser 57474) · ChromaDB 58000. Deliberately non-standard so the stack cannot collide
with a Postgres or Redis already running on your machine.

### Four ways to give it evidence

1. **Demonstration data** — 20 invented companies, identical every run, checkable
   against a known answer.
2. **Generated demonstration data** — the same adversarial cases under companies nobody
   has ever seen, built from a seed you choose. This is the honest test.
3. **Upload your own records** — bank CSV and contract PDFs, through the same parsers
   the live connectors use. No credentials asked for.
4. **Connect your own accounts** — read-only Razorpay / Zoho / HubSpot / Drive.

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
the real HTTP API**, runs every stage in order, then downloads the report and dataset
and opens them to read what is inside. It asserts what a reviewer would otherwise have
to check by hand:

- re-running ingestion creates no duplicate records
- the false-merge trap was not fallen for
- every planted adversarial case was caught
- nothing failing an arithmetic check ever gets published
- a review decision without a written reason is refused
- the evidence chain actually reaches a bank credit
- the audit log's hash chain verifies
- the report is self-contained and contains no accusatory wording
- the downloaded statement's running balance reconciles across all 62 rows
- **the on-screen figure and the downloaded report quote the same number**

Where a check genuinely cannot be made, it prints **SKIP** rather than passing quietly.
A green line that means "not checked" is worse than a red one.

---

## Tech stack

| Layer | Choice |
|---|---|
| Backend | FastAPI, Python 3.13, async throughout |
| Database | PostgreSQL 17, row-level security on all 22 tenant tables |
| Graph | Neo4j 5 — related parties, circular money flows |
| Vectors | ChromaDB — contract clause retrieval |
| Cache | Redis 7 — idempotency keys, locks |
| Matching | OR-Tools CP-SAT solver over integer paise; RapidFuzz |
| Documents | PyMuPDF native parsing, Tesseract OCR only when needed |
| ML | scikit-learn Isolation Forest, gated on measured precision |
| AI | Cerebras `gemma-4-31b` proposes · `gpt-oss-120b` criticises |
| Frontend | Next.js 16, React 19, TypeScript, Tailwind 4 |
| Testing | pytest, Hypothesis property tests, Playwright |

---

## Security

- **Tenant isolation** via PostgreSQL row-level security — default-deny and `FORCE`d so
  even the table owner cannot bypass it, plus application-level membership checks. A
  non-member gets a **404, not a 403**, so the app never reveals that a workspace
  exists.
- **Provider tokens** are encrypted at rest and never placed in an AI prompt.
- **The audit log** is append-only and SHA-256 hash-chained, with a live integrity
  verdict shown in the UI. Tampering with any historical row breaks the chain and is
  detected.
- **Contract text is treated as hostile** — it was written by the company under review,
  so it is wrapped as untrusted data before any model sees it, and prompt-injection
  attempts are tested for.
- **Webhooks are hints, not data** — verified, deduplicated, then the authoritative
  record is re-fetched from the provider.
- **Uploads** are checked on magic bytes, size and page count; disguised executables,
  ZIPs and NUL bytes are rejected.

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

`backend/Dockerfile` packages it — measured at 140 MB idle and 410 MB peak, so it fits
a 512 MB instance with headroom. It runs on any container host, or locally behind a
tunnel:

```bash
cd frontend
echo "https://<tunnel-url>" | vercel env add NEXT_PUBLIC_API_BASE production
vercel --prod    # the API URL is compiled into the bundle, so redeploy after changing it
```

Set `EXTRA_ALLOWED_ORIGINS` in `.env` to your Vercel URL, or the browser will block
every request.

---

## Known limits

Stated plainly, because a diligence tool that hides its own gaps has no business
pointing out anyone else's:

- **The ARR policy is not accountant-reviewed.** It is RevenueProof's stated, versioned
  policy — not an accounting standard, and it does not claim to be IFRS 15.
- **Identity matching weights are hand-set**, not trained on labelled pairs for a real
  workspace, so automatic merging stays disabled and borderline matches go to review.
- **Some records cannot be linked at all** — a bank narration with no tax ID, domain or
  email cannot be matched on a name alone, and correctly lands in review rather than
  being guessed.
- **Account Aggregator** support stays an adapter behind the bank-CSV contract; real AA
  access needs partner onboarding.
- **Three anomaly rules are covered by unit tests only** — the demo dataset happens not
  to trigger them, so their thresholds are unvalidated against real data.
