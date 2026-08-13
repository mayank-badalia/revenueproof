# RevenueProof

**Live:** https://revenueproof.vercel.app · **Code:** https://github.com/mayank-badalia/revenueproof

The frontend is deployed. The API runs on a machine and is reached through a tunnel —
see *Deployment* below for why, and `./scripts/serve-demo.sh` to bring it up.

Checks whether a startup's claimed revenue is supported by evidence.

A founder states a figure. RevenueProof assembles the chain behind it —
`Customer → Contract → Invoice → Payment → Bank Receipt → Refund` — classifies every
amount into one of eight states, has an independent critic argue against each
material classification, sends what it cannot settle to a human, and publishes only
what survives. Every published rupee traces back to the contract clause, the payment
event and the rule that produced it.

**It does not certify revenue and it is not investment advice.** It shows what the
evidence supports, and what it does not.

---

## What it actually does

On the built-in demonstration dataset, a full run takes about a minute:

| Stage | Result |
|---|---|
| Ingestion | 234 records across 5 sources |
| Customer identity | 24 customers from 75 records; a false merge prevented |
| Contract reading | 13 of 14 read, every amount carrying a verified page citation |
| Cash reconciliation | solver OPTIMAL, conservation verified to the paisa |
| Revenue truth | 64 items across 7 states |
| Anomaly detection | 24 indicators across 10 rules |
| Adversarial critic | 41 published, 22 disputed, 23 routed to a person |
| Diligence room | version published, every figure traceable |

Claimed ₹1,50,00,000 · evidence-supported ₹1,36,81,000 · **91.2%**. The 8.8% gap is
the dataset's deliberately planted cases: a refunded payment, an unpaid invoice, cash
with no invoice behind it, contracted value never billed.

### The cases it is built to catch

The demonstration dataset plants all of these, and the detectors find them on
companies they have never seen:

- One customer spelled four ways across four systems — merged
- Two companies with near-identical names — **not** merged, on conflicting tax IDs
- A one-time implementation fee sold as an annual subscription — split correctly
- A payment refunded days later — not counted
- One agent settling for two customers — both found
- Money that arrives and leaves again — the round trip closed
- Cash with no invoice or contract — reported, never counted as revenue

---

## Running it

### 1. Infrastructure

```bash
# macOS: colima start --cpu 4 --memory 8
cd infra && docker compose up -d    # postgres, redis, neo4j, chromadb
```

### 2. Backend

```bash
cd backend
uv venv --python 3.13 && uv pip install -e ".[dev]"
cp ../.env.example ../.env          # add CEREBRAS_API_KEY to enable the agents
.venv/bin/python -m uvicorn app.main:app --port 8000
```

Without a model key every financial figure still computes — the money engine,
reconciliation, classification and the rule-based detectors are pure Python. Contract
reading and the critic's prose are what need a model.

### 3. Frontend

```bash
cd frontend && npm install && npm run dev
```

Open http://localhost:3000, register, create a workspace, choose an evidence source,
press **Run everything**.

### One command instead

```bash
./scripts/serve-demo.sh
```

Brings up Docker, the API and a public Cloudflare tunnel, and prints the URL to point
a deployed frontend at.

---

## Verifying it yourself

```bash
cd backend && .venv/bin/python -m pytest -q          # 531 tests
.venv/bin/python scripts/verify_all.py               # 93 end-to-end checks
```

`verify_all.py` builds a workspace from nothing through the real HTTP API, runs every
feature, downloads the artefacts and opens them. It asserts what a reviewer would
otherwise check by hand: that re-ingestion creates no duplicates, that a false merge
was prevented, that the planted cases were caught, that nothing failing a
deterministic check is published, that the evidence chain reaches a bank credit, that
the audit hash chain verifies, and that the downloaded statement's running balance
reconciles across all 62 rows.

Where a check cannot be made it prints SKIP rather than passing quietly.

---

## How it is put together

| Layer | Choice |
|---|---|
| Backend | FastAPI, Python 3.13, async throughout |
| Database | PostgreSQL 17 with row-level security on all 22 tenant tables |
| Graph | Neo4j — related parties, circular flows |
| Allocation | OR-Tools CP-SAT over integer minor units |
| ML | scikit-learn Isolation Forest, gated on its own measured precision |
| Models | Cerebras `gemma-4-31b` (proposer) + `gpt-oss-120b` (critic) |
| Frontend | Next.js 16, React 19, TypeScript, Tailwind 4 |

### Four decisions worth knowing

**Money is never a float.** Integer minor units everywhere, including the frontend,
which renders backend-formatted strings and does no arithmetic. `₹1.00` split three
ways conserves exactly.

**Deterministic rules outrank the model, always.** Arithmetic, dates, currency, refund
subtraction and permissions are pure Python. The model reads unstructured contract
language and argues against classifications; it cannot move a number. Measured over
three runs on identical evidence, the classifier produced the same figure every time.

**The critic cannot silently veto.** It reads original evidence rather than the
proposer's summary, comes from a different model family, and can only ever weaken a
classification. What it cannot do is withhold a figure that passed every arithmetic
check — an irreproducible verdict must not move a published number, so its objection
is recorded and routed to a person instead.

**Uncertainty goes to a human, with the question collapsed.** Equivalent questions
become one decision covering many records; answering it resolves them all, each
keeping its own audit entry.

---

## Deployment

The frontend deploys to Vercel. The backend does not, and the reason is arithmetic
rather than preference: 619 MB of Python dependencies against a 250 MB function
limit, four stateful services, a WebSocket for the live trace, and runs that take
47–113 seconds against a 60-second limit. It runs on a machine, exposed through a
tunnel or a container host.

### Run it

```bash
./scripts/serve-demo.sh
```

Docker, the API and a public Cloudflare tunnel in one command. It prints the tunnel
URL and the two commands that point the deployed frontend at it:

```bash
cd frontend
echo "https://<the-tunnel-url>" | vercel env add NEXT_PUBLIC_API_BASE production
vercel --prod    # the API base is compiled into the bundle, so redeploy after changing it
```

Two consequences worth knowing. The tunnel URL changes every run, so those two
commands are repeated each time. And the API only answers while the machine running
`serve-demo.sh` is awake — this suits a demo you are present for rather than a link
someone opens later.

Set `EXTRA_ALLOWED_ORIGINS` in `.env` to the Vercel URL, or the browser refuses every
request from it.

---

## Security

- Tenant isolation is row-level security, default-deny, `FORCE`d so even the table
  owner cannot bypass it — plus application-layer membership checks. A non-member
  gets 404 rather than 403, so workspace existence is not disclosed.
- Provider tokens are encrypted at rest and never placed in a prompt.
- The audit log is append-only and SHA-256 hash-chained; the UI shows a live
  integrity verdict.
- Contract text is written by the company under review, so it is wrapped as untrusted
  data before reaching any prompt.
- Webhooks are hints, not data: signature-verified, deduplicated, then the
  authoritative record is refetched from the provider.

`.env` is gitignored and no credential is committed. Rotate any key that has been
shared in a chat or a screen recording.
