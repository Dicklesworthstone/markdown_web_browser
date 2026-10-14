import { expect, test } from '@playwright/test';

test.describe('Smoke Capture UI shell', () => {
  test('uses the observed pinned build and renders stable viewport text', async ({ page, browser }, testInfo) => {
    const metadata = testInfo.config.metadata;
    const observedBuild = browser.version();
    testInfo.annotations.push({ type: 'browser-build', description: observedBuild });
    testInfo.annotations.push({ type: 'browser-transport', description: 'cdp' });
    testInfo.annotations.push({ type: 'playwright-version', description: metadata.playwrightVersion });
    if (metadata.playwrightChannel === 'cft') {
      expect(observedBuild).toBe(metadata.cftExpectedBuild);
    }
    expect(metadata.playwrightTransport).toBe('cdp');
    await page.setContent(`
      <style>
        body { margin: 0; background: white; font: 28px sans-serif; }
        @keyframes moving { from { transform: translateX(0); } to { transform: translateX(200px); } }
        #moving { width: 40px; height: 40px; background: blue; animation: moving 0.3s linear infinite; }
      </style>
      <h1>Capture text 7319</h1>
      <input aria-label="Caret canary" value="Typing text" autofocus>
      <div id="moving"></div>
      <div data-volatile>first value</div>
    `);
    await page.locator('input').focus();
    const glyphPixels = await page.evaluate(() => {
      const canvas = document.createElement('canvas');
      canvas.width = 320;
      canvas.height = 60;
      const context = canvas.getContext('2d');
      if (!context) throw new Error('Canvas text probe unavailable');
      context.font = '28px sans-serif';
      context.fillText('Capture text 7319', 0, 40);
      const pixels = context.getImageData(0, 0, 320, 60).data;
      let ink = 0;
      for (let index = 3; index < pixels.length; index += 4) {
        if (pixels[index] > 0) ink += 1;
      }
      return ink;
    });
    expect(glyphPixels).toBeGreaterThan(100);
    testInfo.annotations.push({ type: 'glyph-pixels', description: String(glyphPixels) });
    const initialPosition = await page.locator('#moving').evaluate((element) => element.getBoundingClientRect().x);
    await page.waitForTimeout(170);
    const movedPosition = await page.locator('#moving').evaluate((element) => element.getBoundingClientRect().x);
    expect(movedPosition).not.toBe(initialPosition);
    const masks = [page.locator('[data-volatile]'),
      ...(metadata.screenshotMaskSelectors ?? []).map((selector: string) => page.locator(selector))];
    const options = { animations: 'disabled' as const, caret: 'hide' as const, fullPage: false, mask: masks };
    const screenshotPath = testInfo.outputPath('frozen-viewport.png');
    const first = await page.screenshot({ ...options, path: screenshotPath });
    await page.locator('[data-volatile]').evaluate((element) => { element.textContent = 'second value'; });
    await page.waitForTimeout(170);
    const repeated = await page.screenshot(options);
    expect(repeated.equals(first)).toBe(true);
    await page.locator('h1').evaluate((element) => { (element as HTMLElement).style.visibility = 'hidden'; });
    const noText = await page.screenshot(options);
    expect(noText.equals(first)).toBe(false);
    await testInfo.attach('frozen-viewport', { path: screenshotPath, contentType: 'image/png' });
  });

  test('renders toolbar + tabs', async ({ page, baseURL }) => {
    const target = baseURL ?? 'http://localhost:8000';
    await page.goto(target, { waitUntil: 'domcontentloaded' });

    await expect(page.getByRole('textbox', { name: /url/i })).toBeVisible();
    await expect(page.getByRole('button', { name: /run/i })).toBeVisible();
    await expect(page.getByRole('tab', { name: /rendered markdown/i })).toBeVisible();
    await expect(page.getByRole('tab', { name: /raw markdown/i })).toBeVisible();
  });
});
