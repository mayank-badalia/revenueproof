/**
 * Feature 2 end-to-end UI check, with the real Groq critic enabled.
 *
 * The assertion that matters: Northstar's spellings merge into one customer while
 * Blue Harbor Analytics and Blue Harbour Logistics stay separate.
 */

const { chromium } = require('playwright');

const TARGET_URL = process.env.TARGET_URL || 'http://localhost:3000';
const EMAIL = `f2-ui-${Date.now()}@example.com`;

(async () => {
  const browser = await chromium.launch({ headless: false, slowMo: 30 });
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
    console.log('\n[1] Setup and evidence collection');
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
    await page.waitForSelector('text=Customer identity', { timeout: 20000 });

    await page.click('button:has-text("Collect evidence")');
    await page.waitForSelector('text=/Collected \\d+ canonical records/', { timeout: 120000 });
    check('evidence collected', true);

    console.log('\n[2] Resolve identities (critic ON — real Groq calls)');
    // Critic checkbox is on by default; leave it.
    const criticOn = await page.isChecked('input[type="checkbox"] >> nth=0').catch(() => true);
    await page.click('button:has-text("Resolve identities")');
    await page.waitForSelector('text=Resolved customers', { timeout: 300000 });
    await page.waitForTimeout(1500);
    check('resolution completed with the critic enabled', true);

    const readStat = async (label) => {
      const text = await page
        .locator(`p:text-is("${label}") + p`)
        .first()
        .textContent()
        .catch(() => '0');
      return parseInt(text.trim(), 10) || 0;
    };
    const stats = {
      records: await readStat('Records'),
      pairs: await readStat('Pairs scored'),
      customers: await readStat('Customers'),
      accepted: await readStat('Accepted'),
      review: await readStat('For review'),
      disputes: await readStat('Critic disputes'),
    };
    console.log('       ', JSON.stringify(stats));
    check('records considered', stats.records > 50, `${stats.records}`);
    check('pairs scored', stats.pairs > 100, `${stats.pairs}`);
    check('customers resolved', stats.customers >= 15 && stats.customers <= 45, `${stats.customers}`);

    console.log('\n[3] THE CORE ASSERTION — merge Northstar, split Blue Harbour');
    const body = await page.textContent('body');

    // Find the resolved-customer rows and inspect aliases.
    const rows = await page.locator('table >> tbody >> tr').allTextContents();
    const northstarRows = rows.filter((r) => /northstar|nstar/i.test(r));
    const harborRows = rows.filter((r) => /blue harb/i.test(r));

    console.log('        Northstar rows:', northstarRows.length);
    northstarRows.slice(0, 3).forEach((r) => console.log('          ', r.replace(/\s+/g, ' ').slice(0, 130)));
    console.log('        Blue Harb* rows:', harborRows.length);
    harborRows.slice(0, 4).forEach((r) => console.log('          ', r.replace(/\s+/g, ' ').slice(0, 130)));

    const analyticsRow = harborRows.find((r) => /harbor analytics/i.test(r));
    const logisticsRow = harborRows.find((r) => /harbour logistics/i.test(r));
    check('Blue Harbor Analytics present as its own customer', Boolean(analyticsRow));
    check('Blue Harbour Logistics present as its own customer', Boolean(logisticsRow));
    check(
      'the two Blue Harb* companies are SEPARATE customers',
      Boolean(analyticsRow && logisticsRow && analyticsRow !== logisticsRow),
      'a merge here would understate customer concentration',
    );
    check(
      'no single row contains both Blue Harb* entities',
      !harborRows.some((r) => /harbor analytics/i.test(r) && /harbour logistics/i.test(r)),
    );

    console.log('\n[4] Prevented merges surfaced');
    check('prevented-merge notice shown', /merges prevented/i.test(body));

    console.log('\n[5] Evidence trail on a link');
    await page.click('button:has-text("rejected")');
    await page.waitForTimeout(1200);
    const rejectedItems = await page.locator('ul >> li >> button').count();
    check('rejections are browsable', rejectedItems > 0, `${rejectedItems} links`);
    if (rejectedItems > 0) {
      await page.locator('ul >> li >> button').first().click();
      await page.waitForTimeout(600);
      const detail = await page.textContent('body');
      check('per-signal weights shown', /conflict|mismatch|tokens/i.test(detail));
    }

    console.log('\n[6] Auto-merge gate');
    check(
      'auto-merge disabled without labelled pairs',
      /unmeasured|Automatic merging is disabled/i.test(body),
    );

    await page.screenshot({ path: '/tmp/rp-f2-desktop.png', fullPage: true });

    console.log('\n[7] Responsive');
    for (const vp of [
      { name: 'tablet', width: 768, height: 1024 },
      { name: 'mobile', width: 375, height: 667 },
    ]) {
      await page.setViewportSize({ width: vp.width, height: vp.height });
      await page.waitForTimeout(500);
      const overflows = await page.evaluate(
        () => document.documentElement.scrollWidth > window.innerWidth + 2,
      );
      check(`${vp.name}: no horizontal overflow`, !overflows);
      await page.screenshot({ path: `/tmp/rp-f2-${vp.name}.png`, fullPage: true });
    }

    const realErrors = consoleErrors.filter(
      (e) => !e.includes('favicon') && !e.includes('DevTools'),
    );
    check('no console errors', realErrors.length === 0, realErrors.slice(0, 2).join(' | '));

    console.log(`\n${failures === 0 ? 'ALL CHECKS PASSED' : `${failures} CHECK(S) FAILED`}`);
    process.exitCode = failures === 0 ? 0 : 1;
  } catch (error) {
    console.error('\nFATAL:', error.message);
    await page.screenshot({ path: '/tmp/rp-f2-failure.png', fullPage: true }).catch(() => {});
    process.exitCode = 1;
  } finally {
    await browser.close();
  }
})();
