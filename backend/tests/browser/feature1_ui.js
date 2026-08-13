/**
 * Feature 1 end-to-end UI check (Step 2a categories 8 and 10).
 *
 * Triggers evidence collection exactly as a founder would, and verifies the
 * results actually render on the site — not just in the terminal.
 */

const { chromium } = require('playwright');

const TARGET_URL = process.env.TARGET_URL || 'http://localhost:3001';
const EMAIL = `f1-ui-${Date.now()}@example.com`;
const PASSWORD = 'diligence-2026';

(async () => {
  const browser = await chromium.launch({ headless: false, slowMo: 40 });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

  const consoleErrors = [];
  page.on('console', (m) => m.type() === 'error' && consoleErrors.push(m.text()));
  page.on('pageerror', (e) => consoleErrors.push(`pageerror: ${e.message}`));

  let failures = 0;
  const check = (label, ok, detail = '') => {
    console.log(`${ok ? '  ok  ' : ' FAIL '} ${label}${detail ? ` — ${detail}` : ''}`);
    if (!ok) failures++;
  };

  try {
    // --- setup: register + create workspace ------------------------------
    console.log('\n[1] Setup');
    await page.goto(TARGET_URL, { waitUntil: 'domcontentloaded' });
    await page.click('text=Need an account? Register');
    await page.fill('#email', EMAIL);
    await page.fill('#password', PASSWORD);
    await page.click('button[type="submit"]');
    await page.waitForSelector('text=Review workspaces', { timeout: 15000 });

    await page.click('text=New workspace');
    await page.fill('#company_name', 'Northstar Diligence Demo Private Limited');
    await page.fill('#period_start', '2026-04-01');
    await page.fill('#period_end', '2027-03-31');
    await page.fill('#claimed_revenue', '10000000.00');
    await page.fill('#claimed_arr', '10000000.00');
    await page.click('button:has-text("Create workspace")');
    await page.waitForSelector('text=Northstar Diligence Demo', { timeout: 15000 });
    await page.click('text=Northstar Diligence Demo Private Limited');
    await page.waitForSelector('text=Evidence vault', { timeout: 15000 });
    check('workspace created and dashboard reached', true);

    // --- 2. Collect evidence ---------------------------------------------
    console.log('\n[2] Collect evidence (Feature 1 end to end)');
    const before = await page.textContent('body');
    check('starts with no evidence', before.includes('No evidence yet'));

    await page.click('button:has-text("Collect evidence")');
    // Ingestion runs ~4 sources + bank CSV; allow generous time.
    await page.waitForSelector('text=/Collected \\d+ canonical records/', {
      timeout: 90000,
    });
    const notice = await page.textContent('text=/Collected \\d+ canonical records/');
    check('ingestion completed with a result banner', true, notice.trim());

    await page.waitForTimeout(1500);
    const after = await page.textContent('body');

    // --- 3. Results render on the site ------------------------------------
    console.log('\n[3] Results visible on the live site');
    const counts = {};
    for (const label of ['Invoices', 'Payments', 'Refunds', 'Bank transactions', 'Contracts', 'Customers']) {
      const value = await page
        .locator(`dt:has-text("${label}") + dd`)
        .first()
        .textContent()
        .catch(() => '0');
      counts[label] = parseInt(value.trim(), 10) || 0;
    }
    console.log('       evidence counts:', JSON.stringify(counts));

    check('invoices ingested (expect 55)', counts['Invoices'] === 55, `${counts['Invoices']}`);
    check('payments ingested (expect 56)', counts['Payments'] === 56, `${counts['Payments']}`);
    check('bank transactions ingested (expect 62)', counts['Bank transactions'] === 62, `${counts['Bank transactions']}`);
    check('contracts ingested (expect 14)', counts['Contracts'] === 14, `${counts['Contracts']}`);
    check('refunds incl. chargeback (expect 4)', counts['Refunds'] === 4, `${counts['Refunds']}`);

    // --- 4. Per-source table with synthetic honesty -----------------------
    console.log('\n[4] Per-source breakdown and credential honesty');
    check('per-source table rendered', after.includes('Razorpay') && after.includes('Zoho Books'));
    check('synthetic mode labelled', after.includes('synthetic'),
      'sources without credentials must be badged');
    check('vault inventory shown', after.includes('Vaulted evidence'));

    // --- 5. Provenance hashes --------------------------------------------
    console.log('\n[5] Provenance');
    await page.click('summary:has-text("Show provenance hashes")');
    await page.waitForTimeout(600);
    const hashRows = await page.locator('td.px-2.py-1.text-slate-500').count();
    check('content hashes displayed', hashRows > 0, `${hashRows} hash cells`);

    // --- 6. Live trace captured the run ----------------------------------
    console.log('\n[6] Live trace');
    const traceText = await page
      .locator('section:has-text("Processing trace")')
      .first()
      .textContent();
    check('trace shows connector activity', /Connector Agent|Razorpay|Bank CSV/.test(traceText));
    const eventCount = traceText.match(/(\d+) events/)?.[1] ?? '0';
    check('trace populated', parseInt(eventCount, 10) > 5, `${eventCount} events`);

    // --- 7. Idempotency visible in the UI --------------------------------
    console.log('\n[7] Re-run is idempotent');
    await page.click('button:has-text("Collect evidence")');
    await page.waitForTimeout(6000);
    const rerun = await page.textContent('body');
    const dupMatch = await page
      .locator('table >> text=/^\\d+$/')
      .count()
      .catch(() => 0);
    const countsAfter = {};
    for (const label of ['Invoices', 'Payments', 'Bank transactions']) {
      const value = await page
        .locator(`dt:has-text("${label}") + dd`)
        .first()
        .textContent()
        .catch(() => '0');
      countsAfter[label] = parseInt(value.trim(), 10) || 0;
    }
    console.log('       counts after re-run:', JSON.stringify(countsAfter));
    check(
      're-running created no duplicate records',
      countsAfter['Invoices'] === counts['Invoices'] &&
        countsAfter['Payments'] === counts['Payments'] &&
        countsAfter['Bank transactions'] === counts['Bank transactions'],
    );

    await page.screenshot({ path: '/tmp/rp-f1-desktop.png', fullPage: true });

    // --- 8. Cross-device --------------------------------------------------
    console.log('\n[8] Responsive');
    for (const vp of [
      { name: 'tablet', width: 768, height: 1024 },
      { name: 'mobile', width: 375, height: 667 },
    ]) {
      await page.setViewportSize({ width: vp.width, height: vp.height });
      await page.waitForTimeout(600);
      const overflows = await page.evaluate(
        () => document.documentElement.scrollWidth > window.innerWidth + 2,
      );
      const usable = await page.locator('button:has-text("Collect evidence")').isVisible();
      check(`${vp.name} (${vp.width}px): no horizontal overflow`, !overflows);
      check(`${vp.name}: collect button reachable`, usable);
      await page.screenshot({ path: `/tmp/rp-f1-${vp.name}.png`, fullPage: true });
    }

    // --- 9. Console -------------------------------------------------------
    const realErrors = consoleErrors.filter(
      (e) => !e.includes('favicon') && !e.includes('DevTools'),
    );
    check('no console errors', realErrors.length === 0, realErrors.slice(0, 2).join(' | '));

    console.log(`\n${failures === 0 ? 'ALL CHECKS PASSED' : `${failures} CHECK(S) FAILED`}`);
    process.exitCode = failures === 0 ? 0 : 1;
  } catch (error) {
    console.error('\nFATAL:', error.message);
    await page.screenshot({ path: '/tmp/rp-f1-failure.png', fullPage: true }).catch(() => {});
    process.exitCode = 1;
  } finally {
    await browser.close();
  }
})();
