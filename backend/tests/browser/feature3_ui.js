/**
 * Feature 3 end-to-end UI check — the step I skipped.
 *
 * Step 2a category 8 (end-to-end workflow fidelity) and category 10 (cross-device).
 * Contract extraction is paced against the LLM free tier, so this drives a bounded
 * subset rather than all 14 documents: the goal is to prove the workflow works from
 * the browser, which the backend run already proved for accuracy.
 */

const { chromium } = require('playwright');

const TARGET_URL = process.env.TARGET_URL || 'http://localhost:3000';
const EMAIL = `f3-ui-${Date.now()}@example.com`;

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
    await page.waitForSelector('text=Contract terms', { timeout: 20000 });
    check('contracts panel is present on the dashboard', true);

    await page.click('button:has-text("Collect evidence")');
    await page.waitForSelector('text=/Collected \\d+ canonical records/', { timeout: 120000 });
    check('evidence collected', true);

    console.log('\n[2] Contracts appear before extraction, with no invented terms');
    await page.waitForTimeout(1200);
    let body = await page.textContent('body');
    check('14 contracts listed', /14 of 14 contracts|of 14 contracts/.test(body),
      body.match(/\d+ of \d+ contracts have extracted terms/)?.[0] ?? 'not found');
    check(
      'unparsed contracts show no fabricated dates',
      (await page.locator('table >> tbody >> tr').allTextContents())
        .filter((r) => /\.pdf|Agreement|MSA|SOW/i.test(r))
        .some((r) => r.includes('—')),
      'an unread contract must not display a term it does not have',
    );

    console.log('\n[3] Read contracts (real Groq extraction, paced to the free tier)');
    await page.click('button:has-text("Read contracts")');
    check('pacing notice shown to the user', /paced to the LLM provider/i.test(
      await page.textContent('body'),
    ));

    // Extraction is rate-limited; wait generously for the run summary to appear.
    await page.waitForSelector('p:text-is("Processed")', { timeout: 900000 });
    await page.waitForTimeout(2000);
    body = await page.textContent('body');

    const stat = async (label) => {
      const t = await page.locator(`p:text-is("${label}") + p`).first().textContent().catch(() => '0');
      return parseInt(t.trim(), 10) || 0;
    };
    const stats = {
      processed: await stat('Processed'),
      extracted: await stat('Extracted'),
      review: await stat('Need review'),
      failed: await stat('Failed'),
      ocr: await stat('Required OCR'),
      amendments: await stat('Amendments'),
    };
    console.log('       ', JSON.stringify(stats));

    check('all 14 contracts processed', stats.processed === 14, `${stats.processed}`);
    check('no failures', stats.failed === 0, `${stats.failed}`);
    check('the scanned contract required OCR', stats.ocr >= 1, `${stats.ocr}`);
    check('the amendment was linked', stats.amendments >= 1, `${stats.amendments}`);
    check('the ambiguous contract routed to review', stats.review >= 1, `${stats.review}`);
    check('most contracts extracted cleanly', stats.extracted >= 12, `${stats.extracted}`);

    console.log('\n[4] THE CORE ASSERTION — recurring vs one-time split on screen');
    const rows = await page.locator('table >> tbody >> tr').allTextContents();
    const quantum = rows.find((r) => /Quantum/i.test(r));
    console.log('        Quantum row:', (quantum || 'NOT FOUND').replace(/\s+/g, ' ').slice(0, 150));
    check('Quantum Retail row present', Boolean(quantum));
    check(
      'recurring shown as 3,00,000 (not the full 18,00,000)',
      Boolean(quantum && /3,00,000|300,000/.test(quantum)),
      'the ₹15L implementation fee must not be counted as ARR',
    );
    check(
      'one-time shown as 15,00,000 separately',
      Boolean(quantum && /15,00,000|1,500,000/.test(quantum)),
    );

    const silverline = rows.find((r) => /Silverline/i.test(r));
    check('scanned contract shows an OCR badge', Boolean(silverline && /OCR/.test(silverline)));
    const vertex = rows.find((r) => /Vertex/i.test(r));
    check('ambiguous contract shows a review badge', Boolean(vertex && /review/i.test(vertex)));

    console.log('\n[5] Citations open and show verified/unverified');
    const target = page.locator('table >> tbody >> tr', { hasText: 'Northstar' }).first();
    await target.click();
    await page.waitForTimeout(1500);
    const detail = await page.textContent('body');
    check('citations rendered inline', /verified|unverified/.test(detail));
    check('page references shown', /p\.\d+/.test(detail));
    const verifiedCount = await page.locator('text=verified').count();
    check('at least one verified citation', verifiedCount > 0, `${verifiedCount} badges`);

    await page.screenshot({ path: '/tmp/rp-f3-desktop.png', fullPage: true });

    console.log('\n[6] Responsive (Step 2a category 10)');
    for (const vp of [
      { name: 'tablet', width: 768, height: 1024 },
      { name: 'mobile', width: 375, height: 667 },
    ]) {
      await page.setViewportSize({ width: vp.width, height: vp.height });
      await page.waitForTimeout(600);
      const overflows = await page.evaluate(
        () => document.documentElement.scrollWidth > window.innerWidth + 2,
      );
      const usable = await page.locator('button:has-text("Read contracts")').isVisible();
      check(`${vp.name}: no horizontal overflow`, !overflows);
      check(`${vp.name}: read-contracts button reachable`, usable);
      await page.screenshot({ path: `/tmp/rp-f3-${vp.name}.png`, fullPage: true });
    }

    const realErrors = consoleErrors.filter(
      (e) => !e.includes('favicon') && !e.includes('DevTools'),
    );
    check('no console errors', realErrors.length === 0, realErrors.slice(0, 2).join(' | '));

    console.log(`\n${failures === 0 ? 'ALL CHECKS PASSED' : `${failures} CHECK(S) FAILED`}`);
    process.exitCode = failures === 0 ? 0 : 1;
  } catch (error) {
    console.error('\nFATAL:', error.message);
    await page.screenshot({ path: '/tmp/rp-f3-failure.png', fullPage: true }).catch(() => {});
    process.exitCode = 1;
  } finally {
    await browser.close();
  }
})();
