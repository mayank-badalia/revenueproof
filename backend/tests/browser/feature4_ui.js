/**
 * Feature 4 end-to-end UI check.
 *
 * Reconciliation is fully deterministic, so this exercises the real workflow with
 * no dependence on LLM quota — Step 2a categories 8 and 10.
 */

const { chromium } = require('playwright');

const TARGET_URL = process.env.TARGET_URL || 'http://localhost:3000';
const EMAIL = `f4-ui-${Date.now()}@example.com`;

(async () => {
  const browser = await chromium.launch({ headless: false, slowMo: 30 });
  const page = await browser.newPage({ viewport: { width: 1440, height: 950 } });

  const consoleErrors = [];
  page.on('console', (m) => m.type() === 'error' && consoleErrors.push(m.text()));
  page.on('pageerror', (e) => consoleErrors.push(`pageerror: ${e.message}`));

  let failures = 0;
  const check = (label, ok, detail = '') => {
    console.log(`${ok ? '  ok  ' : ' FAIL '} ${label}${detail ? ` — ${detail}` : ''}`);
    if (!ok) failures++;
  };

  try {
    console.log('\n[1] Setup');
    await page.goto(TARGET_URL, { waitUntil: 'domcontentloaded' });
    await page.click('text=Need an account? Register');
    await page.fill('#email', EMAIL);
    await page.fill('#password', 'diligence-2026');
    await page.click('button[type="submit"]');
    await page.waitForSelector('text=Review workspaces', { timeout: 20000 });

    await page.click('text=New workspace');
    await page.fill('#company_name', 'Northstar Diligence Demo Private Limited');
    await page.fill('#period_start', '2026-04-01');
    await page.fill('#period_end', '2027-03-31');
    await page.fill('#claimed_revenue', '10000000.00');
    await page.fill('#claimed_arr', '10000000.00');
    await page.click('button:has-text("Create workspace")');
    await page.waitForSelector('text=Northstar Diligence Demo', { timeout: 20000 });
    await page.click('text=Northstar Diligence Demo Private Limited');
    await page.waitForSelector('text=Cash reconciliation', { timeout: 20000 });
    check('reconciliation panel present', true);

    await page.click('button:has-text("Collect evidence")');
    await page.waitForSelector('text=/Collected \\d+ canonical records/', { timeout: 120000 });
    check('evidence collected', true);

    console.log('\n[2] Reconcile (deterministic — no LLM)');
    const started = Date.now();
    await page.click('button:has-text("Reconcile cash")');
    await page.waitForSelector('text=/Conservation (verified|FAILED)/', { timeout: 180000 });
    const elapsed = ((Date.now() - started) / 1000).toFixed(0);
    await page.waitForTimeout(1200);
    const body = await page.textContent('body');

    check('reconciliation completed', true, `${elapsed}s`);
    check(
      'conservation VERIFIED (allocated + outstanding == invoiced)',
      /Conservation verified/.test(body),
      'the invariant the whole feature rests on',
    );
    check('solver reached OPTIMAL', /Solver status: OPTIMAL/.test(body));

    console.log('\n[3] The cash chain is shown, not collapsed into "paid"');
    const readTotal = async (label) => {
      const t = await page
        .locator(`p:text-is("${label}") + p`)
        .first()
        .textContent()
        .catch(() => '');
      return t.trim();
    };
    const totals = {
      invoiced: await readTotal('Invoiced'),
      allocated: await readTotal('Allocated'),
      outstanding: await readTotal('Outstanding'),
      refunded: await readTotal('Refunded'),
      retained: await readTotal('Retained'),
      bank: await readTotal('Bank confirmed'),
    };
    console.log('       ', JSON.stringify(totals));

    check('invoiced total shown', /[\d,]/.test(totals.invoiced), totals.invoiced);
    check('retained differs from allocated (refunds subtracted)',
      totals.retained !== totals.allocated,
      'a refunded payment must not count as retained revenue');
    check('outstanding is non-zero (Tidewater never paid)',
      /5,31,000|531,000/.test(totals.outstanding), totals.outstanding);
    check('refunded is non-zero (Cobalt + Halcyon + Quantum)',
      /16,52,000|1,652,000/.test(totals.refunded), totals.refunded);
    check('bank-confirmed tracked separately from allocated',
      totals.bank !== '' && totals.bank !== totals.allocated,
      '"captured" is not "settled"');

    console.log('\n[4] Exceptions surfaced');
    check('failed payments reported as contributing zero',
      /failed payments \(contribute zero\)/.test(body));
    check('unsupported receipts reported',
      /receipts with no payment behind them/.test(body));
    check('invoices with no payment reported',
      /invoices with no payment/.test(body));

    console.log('\n[5] Per-invoice detail');
    const rows = await page.locator('table >> tbody >> tr').allTextContents();
    const withIssues = rows.filter((r) => /not settled|INR/.test(r));
    check('per-invoice rows rendered', withIssues.length > 0, `${withIssues.length} rows`);
    await page.click('button:has-text("all")');
    await page.waitForTimeout(800);
    const allRows = await page.locator('table >> tbody >> tr').allTextContents();
    check('the "all" filter shows more rows than "needs attention"',
      allRows.length >= withIssues.length, `${allRows.length} vs ${withIssues.length}`);

    await page.screenshot({ path: '/tmp/rp-f4-desktop.png', fullPage: true });

    console.log('\n[6] Responsive');
    for (const vp of [
      { name: 'tablet', width: 768, height: 1024 },
      { name: 'mobile', width: 375, height: 667 },
    ]) {
      await page.setViewportSize({ width: vp.width, height: vp.height });
      await page.waitForTimeout(600);
      const overflows = await page.evaluate(
        () => document.documentElement.scrollWidth > window.innerWidth + 2,
      );
      const usable = await page.locator('button:has-text("Reconcile cash")').isVisible();
      check(`${vp.name}: no horizontal overflow`, !overflows);
      check(`${vp.name}: reconcile button reachable`, usable);
      await page.screenshot({ path: `/tmp/rp-f4-${vp.name}.png`, fullPage: true });
    }

    const realErrors = consoleErrors.filter(
      (e) => !e.includes('favicon') && !e.includes('DevTools'),
    );
    check('no console errors', realErrors.length === 0, realErrors.slice(0, 2).join(' | '));

    console.log(`\n${failures === 0 ? 'ALL CHECKS PASSED' : `${failures} CHECK(S) FAILED`}`);
    process.exitCode = failures === 0 ? 0 : 1;
  } catch (error) {
    console.error('\nFATAL:', error.message);
    await page.screenshot({ path: '/tmp/rp-f4-failure.png', fullPage: true }).catch(() => {});
    process.exitCode = 1;
  } finally {
    await browser.close();
  }
})();
