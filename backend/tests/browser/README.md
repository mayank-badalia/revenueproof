# Live browser checks

One script per feature, run against the real running stack — not mocks. They exist
because a passing API test says nothing about whether the number a reviewer reads on
the page is the number the engine computed, and several defects (a synthetic source
labelled "live", a panel that erased its own result, a waterfall that did not add up)
were only ever visible here.

They are plain Playwright scripts rather than pytest cases: they need a browser, a
running frontend and a running backend, so they are deliberately not part of
`pytest -q`.

```bash
# backend on :8000 and frontend on :3000 must already be running
cd ~/.claude/skills/playwright-skill
node run.js /path/to/backend/tests/browser/feature5_ui.js
```

Each script registers its own user and creates its own workspace, so they can be run
repeatedly and in any order. `TARGET_URL` overrides the frontend origin (use it when
Next.js falls back to :3001).

| Script | Covers |
|---|---|
| `base_app_ui.js` | Registration, workspace setup, dashboard, live trace, audit chain |
| `feature1_ui.js` | Evidence collection, provenance hashes, quarantine, bank CSV upload |
| `feature2_ui.js` | Identity resolution, prevented merges, critic evidence trail |
| `feature3_ui.js` | Contract extraction, recurring/one-time split, citation badges |
| `feature4_ui.js` | Conservation verdict, cash-chain totals, per-invoice outcomes |
| `feature5_ui.js` | Claimed vs verified, reconciling waterfall, per-item classification |
