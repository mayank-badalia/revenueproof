# File Structure

One line per meaningful file: what it is, and which feature or sub-feature it serves.
Updated whenever the structure changes, not only when a feature lands.

Legend — **F0** base app · **F1–F8** the eight core features from `idea_features.md` §24.

```
revenueproof/
├── CLAUDE.md                     F0  Project state: status, testing, decisions, how to run
├── file_structure.md             F0  This file
├── .env.example                  F0  Every credential required to run; no secrets
│
├── infra/
│   └── docker-compose.yml        F0  PostgreSQL 17, Redis 7, Neo4j 5, ChromaDB — local stack
│
├── backend/
│   ├── pyproject.toml            F0  Python deps + pytest/ruff config
│   │
│   ├── app/
│   │   ├── main.py               F0  FastAPI entrypoint; boot-time health checks, request
│   │   │                             logging, global error handler, /health
│   │   │
│   │   ├── core/
│   │   │   ├── config.py         F0  Pydantic settings; provider_status() reports which
│   │   │   │                         integrations hold real credentials
│   │   │   ├── events.py         F0  Unified event bus — one emit() writes to the Python
│   │   │   │                         terminal AND streams to the site (Step 1b requirement)
│   │   │   ├── db.py             F0  Async SQLAlchemy engine, sessions, workspace scoping
│   │   │   ├── schema_init.py    F0  Idempotent schema creation + row-level security
│   │   │   │                         (deviation: replaces an Alembic chain — see CLAUDE.md §2)
│   │   │   ├── money.py          F0  Exact integer-minor-unit arithmetic, proration, FX with
│   │   │   │                         pinned rates, conservation invariants. Underpins F3–F6.
│   │   │   ├── crypto.py         F0  SHA-256 provenance hashes, audit hash chaining, webhook
│   │   │   │                         HMAC verification, token encryption, LLM redaction
│   │   │   ├── passwords.py      F0  bcrypt hashing (deviation: replaces passlib — CLAUDE.md §2)
│   │   │   ├── llm.py            F0  Groq structured-output client; separate proposer/critic
│   │   │   │                         models, untrusted-evidence wrapping (OWASP LLM01)
│   │   │   ├── graph_db.py       F0  Neo4j driver, workspace-keyed constraints, ACID writes
│   │   │   ├── cache.py          F0  Redis: idempotency claims, distributed locks, job state
│   │   │   └── rate_limit.py     F0  Sliding-window token budget for LLM calls; honours
│   │   │                             retry-after and provider rate-limit headers
│   │   │
│   │   ├── models/
│   │   │   ├── base.py           F0  Declarative base, UUID/timestamp/workspace mixins
│   │   │   ├── enums.py          F0  Controlled vocabularies — the 8 revenue states, evidence
│   │   │   │                         strength, critic verdicts, statuses, roles
│   │   │   ├── workspace.py      F1  Workspace, User, WorkspaceMember (RBAC), ProviderConnection
│   │   │   ├── evidence.py       F1  RawRecord (immutable + hashed), QuarantinedRecord,
│   │   │   │                         CustomerEntity, Contract, Citation, Invoice, CreditNote,
│   │   │   │                         Payment, Refund, BankTransaction
│   │   │   └── verification.py   F2-F8  VerificationRun, EntityMatchProposal (F2), Allocation
│   │   │                             (F4), RevenueItem (F5), Anomaly (F6), CriticDecision +
│   │   │                             ReviewItem + CorrectionMemory (F7), AuditEvent,
│   │   │                             ReportVersion (F8)
│   │   │
│   │   ├── schemas/
│   │   │   ├── workspace.py      F1  Intake validation for the revenue claim; MoneyOut carries
│   │   │   │                         minor units + decimal + display so the UI never does maths
│   │   │   └── canonical.py      F1  Canonical evidence schemas (sub-feature 5) — Customer,
│   │   │                             Invoice, CreditNote, Payment, Refund, BankTransaction,
│   │   │                             ContractDocument, CrmAccount. Money is always
│   │   │                             (minor units, currency); absent fields stay unknown
│   │   │
│   │   ├── api/
│   │   │   ├── router.py         F0  Aggregate router; feature routers mount here
│   │   │   ├── deps.py           F0  Session, JWT auth, and per-object workspace authorization
│   │   │   │                         (OWASP API1 — every object ID is checked, not just the token)
│   │   │   ├── auth.py           F0  Register / login / me
│   │   │   ├── workspaces.py     F1  Workspace CRUD, summary, membership — sub-feature 1
│   │   │   └── events_api.py     F0  WebSocket + SSE live trace (§10.3), audit log endpoint
│   │   │
│   │   ├── services/
│   │   │   ├── audit.py          F7  Append-only hash-chained audit log + chain verification
│   │   │   ├── vault.py          F1  Provenance vault (sub-feature 6) — immutable RawRecord,
│   │   │   │                         dual SHA-256 (canonical JSON + original bytes), versioning
│   │   │   │                         on change, W3C PROV lineage, object storage
│   │   │   ├── quarantine.py     F1  Data Quality Agent (sub-feature 7) — schema validation,
│   │   │   │                         classified rejection reasons, near-duplicate detection
│   │   │   └── ingestion.py      F1  Pipeline orchestration: fetch → vault → normalise →
│   │   │                             validate → upsert. Idempotency lock + canonical upserts
│   │   │
│   │   ├── connectors/
│   │   │   ├── base.py           F1  Connector protocol (sub-features 2-3): synthetic fallback,
│   │   │   │                         cursor-based incremental sync, bounded pagination
│   │   │   ├── providers.py      F1  Razorpay, Zoho Books, Google Drive, HubSpot clients built
│   │   │   │                         to the documented API shapes; webhook HMAC verification
│   │   │   ├── normalize.py      F1  Deterministic provider→canonical mapping (sub-feature 5);
│   │   │   │                         one-time/recurring/ambiguous line hints; bank column aliases
│   │   │   ├── bank_csv.py       F1  Bank CSV adapter (sub-feature 4) — OWASP upload checks,
│   │   │   │                         header detection, formula-injection guard, per-row rejection
│   │   │   └── synthetic/        F1  Demonstration dataset (spec §15)
│   │   │       ├── customers.py      20 customers with cross-system naming variations,
│   │   │       │                     related-party, near-duplicate and shared-account cases
│   │   │       ├── contracts.py      14 contracts rendered as real PDFs (one image-only,
│   │   │       │                     one ambiguous, one future-period, one amendment)
│   │   │       ├── transactions.py   Invoices/payments/refunds/bank rows in PROVIDER-NATIVE
│   │   │       │                     shapes, so the normalisers are genuinely exercised
│   │   │       └── generator.py      Seeded roster generation: same adversarial cases,
│   │   │                             entirely different companies — proof the pipeline
│   │   │                             is not tuned to the fixture
│   │   │
│   │   ├── agents/               F3-F8  (empty) LangGraph agent definitions
│   │   └── features/
│   │       ├── anomaly/          F6  Revenue anomaly and manipulation detection
│   │       │   ├── rules.py          Sub-feature 1: every §Feature 6 pattern as a rule
│   │       │   │                     with a stated threshold and a stated baseline
│   │       │   ├── scoring.py        Sub-features 2, 7: Isolation Forest gated on data
│   │       │   │                     volume, TimeSeriesSplit validation, pinned version
│   │       │   ├── graph.py          Sub-features 3-4: WCC clusters and bounded directed
│   │       │   │                     cycles, with in-process fallbacks for GDS/APOC
│   │       │   ├── concentration.py  Sub-feature 5: top-1/top-N/HHI over verified revenue
│   │       │   ├── explain.py        Sub-feature 6: deterministic reviewer packet; model
│   │       │   │                     prose optional and discarded if accusatory
│   │       │   └── service.py        Sub-features 7-8 + pipeline: parallel scans joined,
│   │       │                         canonical→detector adapters, persistence that
│   │       │                         preserves reviewer verdicts, precision and ML gate
│   │       ├── room/             F8  Living evidence graph and diligence room
│   │       │   ├── evidence.py       Sub-features 1-2: rebuild Customer→Contract→
│   │       │   │                     Invoice→Payment→Bank→Refund for any amount, with
│   │       │   │                     contract quotes, critic verdict and chain breaks
│   │       │   ├── versions.py       Sub-feature 9: immutable report versions and a
│   │       │   │                     diff computed in code, never generated
│   │       │   └── monitor.py        Sub-features 6-8: confirmed-change detection from
│   │       │                         vault versions, impact analysis naming the affected
│   │       │                         customers, and a scoped idempotent rerun
│   │       ├── review/           F7  Adversarial verification and human resolution
│   │       │   ├── critic.py         Sub-features 1-3: deterministic checks that cannot
│   │       │   │                     be overruled, then an independent critic over the
│   │       │   │                     original evidence; APPROVED/DISPUTED/MORE_EVIDENCE,
│   │       │   │                     issue codes routed to the feature that owns them
│   │       │   ├── verify.py         Maker-checker pipeline and the publication gate —
│   │       │   │                     the only code that sets is_published
│   │       │   ├── service.py        Queue summary, ordering, and the decision that
│   │       │   │                     closes an item — reason mandatory, audit-chained,
│   │       │   │                     written to workspace-scoped correction memory
│   │       │   └── report.py         Self-contained HTML report: claim beside evidence,
│   │       │                         every item with its rule and evidence ids, every
│   │       │                         indicator with observed/baseline/what-to-check
│   │       ├── revenue/          F5  Revenue truth and ARR verification
│   │       │   ├── policy.py         Sub-features 1-2: versioned policy, evidence
│   │       │   │                     checklist, rule catalogue with explanations
│   │       │   ├── classify.py       Sub-features 3, 5: ordered decision tree over the
│   │       │   │                     8 states, ARR contribution, double-count detection
│   │       │   └── service.py        Sub-features 4, 6-8 + pipeline: item assembly from
│   │       │                         F2/F3/F4 (invoices, unbilled contracts, and cash
│   │       │                         that no invoice explains), totals, reconciling
│   │       │                         waterfall, concentration
│   │       ├── reconciliation/   F4  Contract-to-cash reconciliation
│   │       │   ├── candidates.py     Sub-features 1, 3: invoice↔payment and
│   │       │   │                     payment↔bank candidate links, scored on amount,
│   │       │   │                     date window, reference and customer
│   │       │   ├── allocation.py     Sub-feature 2: OR-Tools CP-SAT over integer minor
│   │       │   │                     units; makes double-counting structurally impossible
│   │       │   └── service.py        Sub-features 4, 6 + pipeline: refund distribution,
│   │       │                         retained cash, exception routing
│   │       ├── contracts/        F3  Contract revenue intelligence
│   │       │   ├── parsing.py        Sub-features 1-3: safe intake, digital/scanned
│   │       │   │                     classification, native parsing with boxes, OCR fallback,
│   │       │   │                     heading-based clause segmentation
│   │       │   ├── extraction.py     Sub-features 4, 5, 8: targeted clause retrieval,
│   │       │   │                     schema-constrained term extraction, Indian-numeral
│   │       │   │                     amount parsing, citation re-verification
│   │       │   └── service.py        Sub-features 6, 7, 9 + pipeline: period allocation,
│   │       │                         annualisation, amendment precedence, persistence
│   │       └── identity/         F2  Cross-system customer identity
│   │           ├── identifiers.py    Sub-feature 1: cleaning + deterministic exact matching;
│   │           │                     GSTIN→PAN extraction, legal-suffix stripping
│   │           ├── matching.py       Sub-features 2-3: blocking (incl. character n-grams),
│   │           │                     Fellegi–Sunter scoring, cannot-link constrained clustering
│   │           ├── graph.py          Sub-feature 4: Neo4j links, shared-attribute relations,
│   │           │                     bounded neighbourhood queries
│   │           ├── critic.py         Sub-feature 5: Match Critic — deterministic objections,
│   │           │                     materiality gate, different-family model, weaken-only
│   │           └── service.py        Sub-features 6-7 + pipeline: correction memory,
│   │                                 precision/recall evaluation, cluster application
│   │
│   └── tests/
│       ├── conftest.py           F0  Disposes DB/Redis singletons between tests (pytest gives
│       │                             each test a new event loop; production has one)
│       ├── test_money.py         F0  54 tests: functional, boundary, adversarial input, and
│       │                             Hypothesis properties for allocation conservation
│       ├── test_api_workspace.py F1  27 tests: end-to-end API, tenant isolation, audit chain,
│       │                             concurrency, adversarial payloads
│       ├── test_llm.py           F0  16 tests: model independence, prompt-injection resistance,
│       │                             JSON recovery, graceful degradation, real Groq calls
│       ├── test_feature1_ingestion.py
│       │                         F1  45 tests: end-to-end ingestion, idempotency, versioning,
│       │                             normalisation correctness, CSV safety, quarantine,
│       │                             webhook signatures, tenant isolation, dataset integrity
│       ├── test_connectors_live_paths.py
│       │                         F1  22 tests: fetch_live() against documented provider
│       │                             response shapes via httpx MockTransport — auth headers,
│       │                             pagination, cursors, 401/429/500, synthetic boundary
│       ├── test_feature2_identity.py
│       │                         F2  53 tests: identifier cleaning, exact rules, the Northstar
│       │                             merge and Blue Harbour split, transitive false-merge
│       │                             prevention, critic gating, memory isolation, evaluation
│       ├── test_feature3_contracts.py
│       │                         F3  68 tests: safe intake, OCR routing, clause retrieval,
│       │                             Indian-numeral parsing, period allocation, fabricated-
│       │                             citation rejection, amendment precedence
│       ├── test_rate_limit.py    F0  14 tests: provider blocks honoured, per-model
│       │                             budgets, retry-after parsing
│       ├── test_feature4_reconciliation.py
│       │                         F4  23 tests: combined/partial allocation, double-count
│       │                             prevention, refund distribution, ground truth,
│       │                             Hypothesis conservation properties
│       ├── test_feature5_revenue.py
│       │                         F5  42 tests: all 8 revenue states, §19 adversarial
│       │                             cases, policy behaviour, double-count detection,
│       │                             unapplied cash reaching the truth table, a
│       │                             waterfall that reconciles in both directions,
│       │                             nothing published before review
│       ├── test_feature8_room.py
│       │                         F8  10 tests: the chain reaches the bank credit, a
│       │                             break is reported not hidden, the position counts
│       │                             only published figures, an unchanged position
│       │                             creates no version, earlier versions stay readable
│       ├── test_feature7_review.py
│       │                         F7  15 tests: reason mandatory, cross-workspace refusal,
│       │                             audit + correction memory written, one finding one
│       │                             truth across screens, report escaping and wording,
│       │                             seeded download leaks no built-in company
│       ├── test_feature6_anomaly.py
│       │                         F6  55 tests: each rule fires on its case and stays quiet
│       │                             otherwise, forbidden wording enforced over every
│       │                             finding produced, cycle/cluster graph behaviour,
│       │                             one key space across resolved and unresolved records,
│       │                             precision measurement, idempotent re-scan and
│       │                             reviewer verdicts surviving it
│       └── browser/              F0-F5  Live Playwright checks, one script per feature,
│                                     run against the real stack. Not pytest: they need
│                                     a browser, a running API and a running frontend
│
├── frontend/
│   ├── package.json              F0  Next.js 16, React 19, TypeScript, Tailwind 4, Recharts,
│   │                                 React Flow
│   └── src/
│       ├── lib/
│       │   ├── types.ts          F0  Mirrors backend schemas; Money is never a JS number
│       │   └── api.ts            F0  Typed client + auto-reconnecting trace WebSocket
│       ├── components/
│       │   ├── AuthGate.tsx      F0  Sign-in / registration
│       │   ├── ServiceStatus.tsx F0  Dependency health + honest live-credential reporting
│       │   ├── TraceViewer.tsx   F0  Live processing trace (§10.3) — mirrors the terminal
│       │   ├── EvidencePanel.tsx F1  Connect Data (§10.2): trigger ingestion, upload bank CSV,
│       │   │                         per-source results, provenance hashes, quarantine list
│       │   ├── IdentityPanel.tsx F2  Resolved customers with aliases, prevented merges,
│       │   │                         per-signal evidence trail, human accept/reject
│       │   ├── ContractsPanel.tsx F3 Extracted terms with recurring/one-time/future split,
│       │   │                         inline citations with verified/unverified badges
│       │   ├── ReconciliationPanel.tsx
│       │   │                     F4  Conservation verdict, cash-chain totals, per-invoice
│       │   │                         allocated/outstanding/refunded/retained/bank-confirmed
│       │   ├── RevenuePanel.tsx  F5  Claimed vs evidence-supported revenue and ARR, a
│       │   │                         reconciling waterfall where every step names a reason,
│       │   │                         customer concentration, per-item states with rule IDs
│       │   ├── DataSourcePanel.tsx
│       │   │                     F1  Built-in demo / generated demo / connected accounts,
│       │   │                         dataset and report downloads, per-provider connect
│       │   ├── ReviewPanel.tsx   F7  The queue every feature routes uncertainty into;
│       │   │                         decisions disabled until a reason is given
│       │   ├── CriticPanel.tsx   F7  Verdicts, what settled each one, where disputes
│       │   │                         were routed, and what is published
│       │   ├── DiligenceRoom.tsx F8  Position, version history and the evidence chain
│       │   │                         behind any amount; withheld figures with reasons
│       │   └── AnomalyPanel.tsx  F6  Indicators ranked by severity, each showing observed
│       │                             beside baseline, what to check, its limitations and a
│       │                             false-positive control that feeds measured precision
│       └── app/
│           ├── layout.tsx        F0  Root layout
│           ├── page.tsx          F1  Workspace list + setup form (§10.1)
│           └── workspaces/[id]/
│               └── page.tsx      F0  Dashboard: claim cards, evidence inventory, connection
│                                     health, live trace, audit log
│
└── data/
    └── evidence_vault/           F1  Local object storage for vaulted files (gitignored).
                                      Key layout mirrors an S3 prefix scheme so swapping in
                                      S3 + KMS is a driver change, not a redesign
```

## Skeleton code and resource influence

The provided `RevenueProof_Skeleton_Extraction/` folder was read in full before
building. Per build instructions Step 1a it was treated as a resource index and a
floor, not a specification.

| Skeleton element | How it was used |
|---|---|
| `shared/finance.py` (`as_minor_units`, `overlap_days`, `assert_conservation`) | **Extended, not copied.** The concepts carried over into `core/money.py`, which adds a `Money` type, currency-mismatch guards, three-decimal currencies, largest-remainder allocation, pinned FX rates and magnitude bounds. The skeleton's `overlap_days` was already inclusive; that convention was kept and tested explicitly. |
| `shared/security.py` (`sha256_json`, `verify_hmac_sha256`) | **Adopted directly in spirit** — `core/crypto.py` keeps canonical-JSON hashing and constant-time HMAC, and adds audit hash chaining, token encryption and pre-LLM redaction. |
| `shared/contracts.py` (`SubfeatureContext`/`ToolResult` adapter protocol) | **Discarded.** The uniform adapter-list pattern suits a documentation scaffold, but forces unrelated operations into one shape. Real domain services with typed signatures are clearer and testable. |
| 65 × `skeleton.py` | **Used as the resource index.** Each file's `SOURCE_RESOURCES` map names the ranked primary documentation per tool, which drove library selection (Splink over embeddings for entity resolution, OR-Tools CP-SAT for allocation, Document AI over generic OCR, LangGraph interrupts for human review). |
| `manifest.json`, `COVERAGE.md`, `PROJECT_WORKFLOW.md` | Used as the coverage contract: 8 features / 65 sub-features / 153 tool units, and the non-negotiable handoff rules between features. |
