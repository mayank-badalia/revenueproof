/**
 * End-to-end UI check for the RevenueProof base app.
 *
 * Covers Step 2a categories 8 (end-to-end workflow fidelity) and 10 (cross-device
 * functionality): register a real user, create a workspace with a real claim,
 * confirm the dashboard renders backend-computed figures, and confirm the live
 * WebSocket trace actually connects.
 */

const { chromium } = require('playwright');

const TARGET_URL = process.env.TARGET_URL || 'http://localhost:3001';
const EMAIL = `founder-${Date.now()}@example.com`;
const PASSWORD = 'diligence-2026';

const VIEWPORTS = [
  { name: 'desktop', width: 1440, height: 900 },
  { name: 'tablet', width: 768, height: 1024 },
  { name: 'mobile', width: 375, height: 667 },
];

(async () => {
  const browser = await chromium.launch({ headless: false, slowMo: 60 });
  const context = await browser.newContext({ viewport: VIEWPORTS[0] });
  const page = await context.newPage();

  const consoleErrors = [];
  page.on('console', (msg) => {
    if (msg.type() === 'error') consoleErrors.push(msg.text());
  });
  page.on('pageerror', (err) => consoleErrors.push(`pageerror: ${err.message}`));

  let failures = 0;
  const check = (label, ok, detail = '') => {
    console.log(`${ok ? '  ok  ' : ' FAIL '} ${label}${detail ? ` — ${detail}` : ''}`);
    if (!ok) failures++;
  };

  try {
    // --- 1. Registration -------------------------------------------------
    console.log('\n[1] Registration');
    await page.goto(TARGET_URL, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('text=RevenueProof', { timeout: 15000 });

    await page.click('text=Need an account? Register');
    await page.fill('#fullName', 'Playwright Founder');
    await page.fill('#email', EMAIL);
    await page.fill('#password', PASSWORD);
    await page.click('button[type="submit"]');

    await page.waitForSelector('text=Review workspaces', { timeout: 15000 });
    check('registered and landed on workspace list', true);

    // Service status banner must report real backend health.
    const statusText = await page.textContent('body');
    check('service status banner rendered', statusText.includes('PostgreSQL'));
    check(
      'credential honesty shown (no live providers configured)',
      statusText.includes('synthetic dataset') || statusText.includes('Live credentials'),
    );

    // --- 2. Workspace creation ------------------------------------------
    console.log('\n[2] Workspace creation with a real claim');
    await page.click('text=New workspace');
    await page.waitForSelector('#company_name');

    await page.fill('#company_name', 'Northstar Technologies Private Limited');
    await page.fill('#period_start', '2026-04-01');
    await page.fill('#period_end', '2027-03-31');
    await page.fill('#claimed_revenue', '10000000.00');
    await page.fill('#claimed_arr', '10000000.00');
    await page.click('button:has-text("Create workspace")');

    await page.waitForSelector('text=Northstar Technologies', { timeout: 15000 });
    check('workspace created and listed', true);

    // The backend formats money; verify the exact figure surfaced.
    const listText = await page.textContent('body');
    check(
      'claimed revenue formatted by backend (10,000,000.00)',
      listText.includes('10,000,000.00'),
      listText.match(/INR [\d,]+\.\d{2}/)?.[0] || 'not found',
    );

    // --- 3. Dashboard -----------------------------------------------------
    console.log('\n[3] Workspace dashboard');
    await page.click('text=Northstar Technologies Private Limited');
    await page.waitForSelector('text=Processing trace', { timeout: 15000 });
    check('dashboard reached', page.url().includes('/workspaces/'));

    const dashText = await page.textContent('body');
    check('claim cards rendered', dashText.includes('Claimed revenue'));
    check('evidence inventory rendered', dashText.includes('Evidence collected'));
    check('audit log rendered', dashText.includes('Audit log'));
    check(
      'audit hash chain verified',
      dashText.includes('hash chain verified'),
      dashText.match(/hash chain verified \(\d+\)/)?.[0] || 'not shown',
    );
    check(
      'empty-evidence guidance shown',
      dashText.includes('No evidence yet'),
    );

    // --- 4. Live trace socket --------------------------------------------
    console.log('\n[4] Live WebSocket trace');
    // The socket should reach "live" shortly after mount.
    let live = false;
    for (let i = 0; i < 20; i++) {
      const traceText = await page.textContent('body');
      if (traceText.includes('live')) {
        live = true;
        break;
      }
      await page.waitForTimeout(500);
    }
    check('trace socket reported live', live);

    // Trigger backend activity and confirm it streams into the trace panel.
    const eventsBefore = await page
      .locator('section:has-text("Processing trace") >> text=/\\d+ events/')
      .first()
      .textContent()
      .catch(() => '0 events');
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.waitForSelector('text=Processing trace', { timeout: 15000 });
    await page.waitForTimeout(2500);
    const eventsAfter = await page
      .locator('section:has-text("Processing trace") >> text=/\\d+ events/')
      .first()
      .textContent()
      .catch(() => '0 events');
    console.log(`       events: ${eventsBefore} -> ${eventsAfter}`);
    check('trace panel populated with events', !eventsAfter.startsWith('0 '));

    await page.screenshot({ path: '/tmp/rp-desktop.png', fullPage: true });

    // --- 5. Cross-device (Step 2a category 10) ---------------------------
    console.log('\n[5] Responsive functional check');
    for (const viewport of VIEWPORTS) {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      await page.waitForTimeout(600);

      // Functional, not cosmetic: is the content reachable and not clipped?
      const overflows = await page.evaluate(
        () => document.documentElement.scrollWidth > window.innerWidth + 2,
      );
      const traceVisible = await page.locator('text=Processing trace').isVisible();
      check(
        `${viewport.name} (${viewport.width}px): no horizontal overflow`,
        !overflows,
      );
      check(`${viewport.name}: trace panel usable`, traceVisible);

      await page.screenshot({ path: `/tmp/rp-${viewport.name}.png`, fullPage: true });
    }

    // --- 6. Console cleanliness ------------------------------------------
    console.log('\n[6] Browser console');
    const realErrors = consoleErrors.filter(
      (e) => !e.includes('favicon') && !e.includes('Download the React DevTools'),
    );
    check('no console errors', realErrors.length === 0, realErrors.slice(0, 3).join(' | '));

    console.log(
      `\n${failures === 0 ? 'ALL CHECKS PASSED' : `${failures} CHECK(S) FAILED`}`,
    );
    console.log('Screenshots: /tmp/rp-desktop.png, /tmp/rp-tablet.png, /tmp/rp-mobile.png');
    process.exitCode = failures === 0 ? 0 : 1;
  } catch (error) {
    console.error('\nFATAL:', error.message);
    await page.screenshot({ path: '/tmp/rp-failure.png', fullPage: true }).catch(() => {});
    process.exitCode = 1;
  } finally {
    await browser.close();
  }
})();
