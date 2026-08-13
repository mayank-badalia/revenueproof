/**
 * Feature 5 through the UI, against **live provider data**.
 *
 * The sibling `feature5_ui.js` asserts exact figures because it runs on the §15
 * dataset, where the answers are known. This one cannot: the numbers depend on what
 * is in a real Zoho Books organisation and a real Razorpay test account at the moment
 * it runs. So it asserts the things that must hold *whatever* the data says —
 * the waterfall reconciles, refunds never count as verified, sources are labelled by
 * their true origin — and prints the actual figures for a human to read.
 *
 * A test that hard-coded today's live totals would fail tomorrow for the right
 * reason and teach nobody anything.
 */

const { chromium } = require('playwright');

const TARGET_URL = process.env.TARGET_URL || 'http://localhost:3000';
const EMAIL = `f5-live-${Date.now()}@example.com`;

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
    await page.fill('#company_name', 'Live Provider Integration Demo');
    await page.fill('#period_start', '2026-04-01');
    await page.fill('#period_end', '2027-03-31');
    await page.fill('#claimed_revenue', '2000000.00');
    await page.fill('#claimed_arr', '1000000.00');
    await page.click('button:has-text("Create workspace")');
    await page.waitForSelector('text=Live Provider Integration Demo', { timeout: 20000 });
    await page.click('text=Live Provider Integration Demo');
    await page.waitForSelector('text=Revenue truth', { timeout: 20000 });

    console.log('\n[2] Collect from the real provider APIs');
    await page.click('button:has-text("Collect evidence")');
    await page.waitForSelector('text=/Collected \\d+ canonical records/', { timeout: 180000 });
    const collected = await page.textContent('body');
    check('evidence collected from providers', true);

    // The whole point of this run: the sources must not claim to be synthetic.
    const sourceRows = await page.locator('table tbody tr').allTextContents();
    const modeOf = (name) => {
      const row = sourceRows.find((r) => r.toLowerCase().includes(name));
      return row ? (/synthetic/i.test(row) ? 'synthetic' : 'live') : 'absent';
    };
    const modes = {
      razorpay: modeOf('razorpay'),
      zoho: modeOf('zoho'),
      hubspot: modeOf('hubspot'),
    };
    console.log('        source modes:', JSON.stringify(modes));
    check('Zoho reports live, not synthetic', modes.zoho === 'live', modes.zoho);
    check('Razorpay reports live, not synthetic', modes.razorpay === 'live', modes.razorpay);
    check('HubSpot reports live, not synthetic', modes.hubspot === 'live', modes.hubspot);

    console.log('\n[3] Reconcile the real cash');
    await page.click('button:has-text("Reconcile cash")');
    await page.waitForSelector('text=/Conservation (verified|FAILED)/', { timeout: 180000 });
    const reconText = await page.textContent('body');
    check('conservation holds on live data', /Conservation verified/.test(reconText),
      'allocated + outstanding must equal invoiced, to the paisa');
    check('solver reached OPTIMAL', /Solver status: OPTIMAL/.test(reconText));

    console.log('\n[4] Verify revenue');
    await page.click('button:has-text("Verify revenue")');
    await page.waitForSelector('text=Claimed to verified', { timeout: 180000 });
    await page.waitForTimeout(1500);

    const panel = page.locator('section:has(h2:text-is("Revenue truth"))');
    const panelText = await panel.textContent();
    const readPair = async (label) =>
      (await panel.locator(`span:text-is("${label}") + span`).first().textContent().catch(() => '')).trim();

    console.log('        claimed  :', await readPair('Claimed'));
    console.log('        verified :', await readPair('Evidence-supported'));

    const steps = await panel.locator('h3:text-is("Claimed to verified") + ul > li').allTextContents();
    steps.forEach((s) => console.log('         ·', s.replace(/\s+/g, ' ').trim()));

    const parsed = await page.evaluate(() => {
      const section = [...document.querySelectorAll('section')].find(
        (s) => s.querySelector('h2')?.textContent === 'Revenue truth',
      );
      const heading = [...section.querySelectorAll('h3')].find(
        (h) => h.textContent === 'Claimed to verified',
      );
      return [...heading.nextElementSibling.children].map((li) => {
        const spans = li.querySelectorAll(':scope > span');
        return {
          label: spans[0].textContent.trim(),
          amount: Number(spans[spans.length - 1].textContent.replace(/[^\d.]/g, '')),
        };
      });
    });
    const walked = parsed.slice(1, -1).reduce(
      (acc, s) => acc + (s.label.startsWith('−') ? -s.amount : s.amount),
      parsed[0].amount,
    );
    const stated = parsed[parsed.length - 1].amount;
    console.log(`        walked ${walked.toFixed(2)} vs stated ${stated.toFixed(2)}`);
    check('waterfall reconciles on live data', Math.abs(walked - stated) < 0.01);

    console.log('\n[5] Classifications reflect what really happened');
    const rows = panel.locator('table tbody tr');
    const rowCount = await rows.count();
    const classes = {};
    for (const text of await rows.allTextContents()) {
      const m = text.match(/(Verified recurring|Verified one-time|Contracted, unbilled|Invoiced, unpaid|Refunded \/ reversed|Cash without support|Unsupported|Needs review)/);
      if (m) classes[m[1]] = (classes[m[1]] || 0) + 1;
    }
    console.log(`        ${rowCount} items:`, JSON.stringify(classes));
    check('items classified', rowCount > 0, `${rowCount}`);
    check(
      'the refunded Razorpay payments are classified as refunded, not verified',
      (classes['Refunded / reversed'] || 0) > 0,
      'real money went out again — it must not read as revenue',
    );
    check(
      'unpaid invoices are not counted as revenue',
      (classes['Invoiced, unpaid'] || 0) > 0,
    );
    check('nothing double-counted', !/double-count conflicts/.test(panelText));
    check('policy caveat shown', /not an accounting standard/.test(panelText));

    await page.screenshot({ path: '/tmp/rp-f5-live.png', fullPage: true });

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
      check(`${vp.name}: no horizontal overflow`, !overflows);
    }

    const realErrors = consoleErrors.filter(
      (e) => !e.includes('favicon') && !e.includes('DevTools'),
    );
    check('no console errors', realErrors.length === 0, realErrors.slice(0, 2).join(' | '));
    void collected;

    console.log(`\n${failures === 0 ? 'ALL CHECKS PASSED' : `${failures} CHECK(S) FAILED`}`);
    process.exitCode = failures === 0 ? 0 : 1;
  } catch (error) {
    console.error('\nFATAL:', error.message);
    await page.screenshot({ path: '/tmp/rp-f5-live-failure.png', fullPage: true }).catch(() => {});
    process.exitCode = 1;
  } finally {
    await browser.close();
  }
})();
