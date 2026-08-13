/**
 * Feature 5 end-to-end UI check.
 *
 * Revenue verification is deterministic (no LLM calls), so this runs the real
 * workflow regardless of Groq quota — Step 2a categories 8 and 10.
 *
 * The thing under test is not "does a number render". It is whether the page can
 * be *misread*: a claim shown without its evidence, an ARR of zero that looks like
 * "no recurring revenue" when it actually means "contracts unread", or a deduction
 * with no reason attached.
 */

const { chromium } = require('playwright');

const TARGET_URL = process.env.TARGET_URL || 'http://localhost:3000';
const EMAIL = `f5-ui-${Date.now()}@example.com`;

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
    await page.waitForSelector('text=Revenue truth', { timeout: 20000 });
    check('revenue panel present on dashboard', true);

    // Tick demonstration data: this script asserts the §15 dataset's exact figures,
    // which only hold when the dataset is the source. With live provider keys
    // configured, collecting without this reaches into the real accounts instead.
    await page
      .locator('section:has(h2:text-is("Evidence vault"))')
      .locator('label:has-text("demonstration data") input')
      .check();
    await page.click('button:has-text("Collect evidence")');
    await page.waitForSelector('text=/Collected \\d+ canonical records/', { timeout: 120000 });
    check('evidence collected', true);

    await page.click('button:has-text("Reconcile cash")');
    await page.waitForSelector('text=/Conservation (verified|FAILED)/', { timeout: 180000 });
    check('cash reconciled (revenue verification reads its allocations)', true);

    console.log('\n[2] Verify revenue (deterministic — no LLM)');
    const started = Date.now();
    await page.click('button:has-text("Verify revenue")');
    await page.waitForSelector('text=Claimed to verified', { timeout: 180000 });
    const elapsed = ((Date.now() - started) / 1000).toFixed(0);
    await page.waitForTimeout(1200);
    const body = await page.textContent('body');
    check('verification completed', true, `${elapsed}s`);

    console.log('\n[3] The claim never appears without the evidence beside it');
    const panel = page.locator('section:has(h2:text-is("Revenue truth"))');
    const panelText = await panel.textContent();
    const readPair = async (label) => {
      const t = await panel
        .locator(`span:text-is("${label}") + span`)
        .first()
        .textContent()
        .catch(() => '');
      return t.trim();
    };
    const claimed = await readPair('Claimed');
    const verified = await readPair('Evidence-supported');
    const supportedArr = await readPair('Supported (recurring only)');
    console.log('        claimed:', claimed, '| verified:', verified, '| supported ARR:', supportedArr);

    check('claimed revenue shown', /[\d,]/.test(claimed), claimed);
    check('evidence-supported revenue shown', /[\d,]/.test(verified), verified);
    check(
      'verified differs from claimed (the gap is the product)',
      claimed !== verified,
      'a page where they always match proves nothing',
    );
    check(
      'the difference between claim and evidence is named in whichever direction it falls',
      /of the claim has no supporting evidence|Evidence beyond the claim|Claimed but not evidenced/.test(panelText),
      'silently absorbing the difference is how a broken chain hides',
    );

    console.log('\n[4] ARR is a separate claim, and zero is explained');
    check('ARR shown separately from revenue', /Supported \(recurring only\)/.test(body));
    check(
      'zero supported ARR is attributed to unread contracts, not to "no recurring revenue"',
      /contracts have not been read yet/.test(body),
      'the difference between "unknown" and "nothing" is the whole point',
    );

    console.log('\n[5] Every deduction carries a reason');
    const steps = await page.locator('h3:text-is("Claimed to verified") + ul > li').allTextContents();
    console.log('       ', steps.length, 'waterfall steps');
    steps.forEach((s) => console.log('         ·', s.replace(/\s+/g, ' ').trim()));
    check('waterfall rendered', steps.length >= 2, `${steps.length} steps`);
    const deductions = steps.filter((s) => s.trim().startsWith('−'));
    check('deductions marked as subtractions', deductions.length > 0, `${deductions.length}`);
    check(
      'each deduction states why, not just how much',
      deductions.every((s) => /[a-z]{4,}.*\./.test(s)),
      'an amount with no reason is an adjustment, not an argument',
    );
    check(
      'refunded money deducted (fully-refunded Cobalt + Halcyon; Quantum is partial and nets inside its item)',
      /Refunded or reversed/.test(panelText) && /1,298,000|12,98,000/.test(panelText),
    );
    check(
      'invoiced-but-unpaid deducted (Tidewater 5,31,000)',
      /531,000|5,31,000/.test(panelText),
    );

    console.log('\n[5b] The steps actually add up');
    const parsed = await page.evaluate(() => {
      const section = [...document.querySelectorAll('section')].find(
        (s) => s.querySelector('h2')?.textContent === 'Revenue truth',
      );
      const heading = [...section.querySelectorAll('h3')].find(
        (h) => h.textContent === 'Claimed to verified',
      );
      return [...heading.nextElementSibling.children].map((li) => {
        const spans = li.querySelectorAll(':scope > span');
        const label = spans[0].textContent.trim();
        const amount = Number(
          spans[spans.length - 1].textContent.replace(/[^\d.]/g, ''),
        );
        return { label, amount };
      });
    });
    const start = parsed[0].amount;
    const totalRow = parsed[parsed.length - 1];
    const movements = parsed.slice(1, -1);
    const walked = movements.reduce(
      (acc, s) => acc + (s.label.startsWith('−') ? -s.amount : s.amount),
      start,
    );
    console.log('        walked:', walked.toFixed(2), 'vs stated total:', totalRow.amount.toFixed(2));
    check(
      'waterfall reconciles: claimed ± named steps == evidence-supported total',
      Math.abs(walked - totalRow.amount) < 0.01,
      'a reviewer who checks the arithmetic and finds it wrong stops trusting the page',
    );
    check('final step is the supported total', /Evidence-supported revenue/.test(totalRow.label));

    console.log('\n[6] Double-count check and policy honesty');
    check(
      'no false double-count alarms on clean data',
      !/double-count conflicts/.test(body),
      'a detector that fires on every combined payment gets ignored',
    );
    check(
      'policy is stated as RevenueProof policy, not an accounting standard',
      /not an accounting standard/.test(body),
    );
    check('policy version pinned to the result', /Policy v\d/.test(body));

    console.log('\n[7] Per-item classification, with rules and missing evidence');
    const rows = panel.locator('table tbody tr');
    const rowCount = await rows.count();
    check('classified items rendered', rowCount > 0, `${rowCount} rows`);

    const classes = {};
    for (const text of await rows.allTextContents()) {
      const m = text.match(/(Verified recurring|Verified one-time|Contracted, unbilled|Invoiced, unpaid|Refunded \/ reversed|Cash without support|Unsupported|Needs review)/);
      if (m) classes[m[1]] = (classes[m[1]] || 0) + 1;
    }
    console.log('        by class:', JSON.stringify(classes));
    check('at least two distinct states present', Object.keys(classes).length >= 2, Object.keys(classes).join(', '));
    check(
      'cash with no invoice is reported, not dropped (Zenith)',
      (classes['Cash without support'] || 0) > 0,
      'a receipt with no invoice has nothing to hang on and vanishes if items are anchored on invoices',
    );
    check(
      'refunded items classified as refunded, not verified',
      (classes['Refunded / reversed'] || 0) > 0,
      'paid-then-refunded is what a company is most likely to still be counting',
    );

    await rows.first().click();
    await page.waitForTimeout(500);
    const expanded = await rows.first().textContent();
    check('clicking an item reveals the rule that produced it', /R\d{2}_/.test(expanded), (expanded.match(/R\d{2}_[A-Z_]+/) || [''])[0]);

    console.log('\n[8] Filtering by classification');
    const refundFilter = panel.locator('button:has-text("Refunded / reversed (")');
    if (await refundFilter.count()) {
      await refundFilter.first().click();
      await page.waitForTimeout(500);
      const filtered = await rows.count();
      check('filter narrows the table', filtered < rowCount, `${filtered} of ${rowCount}`);
      await panel.locator('button:text-is("all")').click();
      await page.waitForTimeout(500);
      check('"all" restores every row', (await rows.count()) === rowCount);
    } else {
      check('classification filters rendered', false, 'no filter buttons found');
    }

    await page.screenshot({ path: '/tmp/rp-f5-desktop.png', fullPage: true });

    console.log('\n[9] Responsive');
    for (const vp of [
      { name: 'tablet', width: 768, height: 1024 },
      { name: 'mobile', width: 375, height: 667 },
    ]) {
      await page.setViewportSize({ width: vp.width, height: vp.height });
      await page.waitForTimeout(600);
      const overflows = await page.evaluate(
        () => document.documentElement.scrollWidth > window.innerWidth + 2,
      );
      const usable = await page.locator('button:has-text("Verify revenue")').isVisible();
      check(`${vp.name}: no horizontal overflow`, !overflows);
      check(`${vp.name}: verify button reachable`, usable);
      await page.screenshot({ path: `/tmp/rp-f5-${vp.name}.png`, fullPage: true });
    }

    const realErrors = consoleErrors.filter(
      (e) => !e.includes('favicon') && !e.includes('DevTools'),
    );
    check('no console errors', realErrors.length === 0, realErrors.slice(0, 2).join(' | '));

    console.log(`\n${failures === 0 ? 'ALL CHECKS PASSED' : `${failures} CHECK(S) FAILED`}`);
    process.exitCode = failures === 0 ? 0 : 1;
  } catch (error) {
    console.error('\nFATAL:', error.message);
    await page.screenshot({ path: '/tmp/rp-f5-failure.png', fullPage: true }).catch(() => {});
    process.exitCode = 1;
  } finally {
    await browser.close();
  }
})();
