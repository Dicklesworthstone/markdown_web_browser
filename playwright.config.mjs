import { defineConfig } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';
import { createRequire } from 'node:module';

const playwrightVersion = createRequire(import.meta.url)('@playwright/test/package.json').version;
const versionParts = playwrightVersion.split('.').map(Number);
if (versionParts[0] < 1 || (versionParts[0] === 1 && versionParts[1] < 50)) {
  throw new Error(`Playwright >=1.50 is required; found ${playwrightVersion}.`);
}

// Use the application's python-decouple settings rather than a second .env parser.
const browserSettings = JSON.parse(execFileSync('uv', [
  'run', '--no-sync', '--python', '3.13', 'python', '-c',
  `import json
from app.capture import _browser_font_environment
from app.settings import get_settings
s = get_settings().browser
font_environment = _browser_font_environment(s.fontconfig_file)
print(json.dumps({
    "channel": s.playwright_channel,
    "transport": s.browser_transport,
    "cft_version": s.cft_version,
    "cft_label": s.cft_label,
    "executable": str(s.cft_executable) if s.cft_executable else None,
    "fontconfig_file": font_environment.get("FONTCONFIG_FILE") if font_environment else None,
    "viewport_width": s.viewport_width,
    "viewport_height": s.viewport_height,
    "device_scale_factor": s.device_scale_factor,
    "color_scheme": s.color_scheme,
    "mask_selectors": list(s.screenshot_mask_selectors),
    "screenshot_style_hash": s.screenshot_style_hash,
}))`,
], { encoding: 'utf8' }));

const parseIntEnv = (name, fallback) => {
  const raw = process.env[name];
  if (!raw) return fallback;
  const value = parseInt(raw, 10);
  return Number.isFinite(value) ? value : fallback;
};

const parseListEnv = (value, fallback) =>
  (value ?? fallback)
    .split(',')
    .map((entry) => entry.trim())
    .filter((entry) => entry.length > 0);

const viewportWidth = browserSettings.viewport_width;
const viewportHeight = browserSettings.viewport_height;
const deviceScaleFactor = browserSettings.device_scale_factor;
const actionTimeout = parseIntEnv('PLAYWRIGHT_ACTION_TIMEOUT_MS', 15_000);
const navigationTimeout = parseIntEnv('PLAYWRIGHT_NAVIGATION_TIMEOUT_MS', 30_000);
const colorScheme = browserSettings.color_scheme;
const baseURL = process.env.PLAYWRIGHT_BASE_URL ?? process.env.API_BASE_URL ?? 'http://localhost:8000';
const channelCandidates = parseListEnv(browserSettings.channel, 'cft');
if (channelCandidates.length !== 1) {
  throw new Error('Node smoke requires one explicit channel; fallback launch is not implemented here.');
}
const channel = channelCandidates[0];

const transportCandidates = parseListEnv(browserSettings.transport, 'cdp')
  .map((entry) => entry.toLowerCase());
if (transportCandidates.some((entry) => !['cdp', 'bidi'].includes(entry)) ||
    !transportCandidates.includes('cdp')) {
  throw new Error('BiDi is not implemented by this smoke adapter; select cdp.');
}
const maskSelectors = browserSettings.mask_selectors;
const build = browserSettings.cft_version.replace(/^chrome-/, '');
if (channel === 'cft' && !/^\d+\.\d+\.\d+\.\d+$/.test(build)) {
  throw new Error('CFT_VERSION must name an exact four-part Chrome build.');
}
const cftExecutable = resolve(browserSettings.executable ??
  join(homedir(), '.cache/mdwb/cft', build, 'chrome-linux64/chrome'));
if (channel === 'cft' && !existsSync(cftExecutable)) {
  throw new Error(`Pinned CfT executable is missing: ${cftExecutable}. Install that build or set CFT_EXECUTABLE.`);
}
const observedBinaryBuild = channel === 'cft'
  ? execFileSync(cftExecutable, ['--version'], { encoding: 'utf8' })
    .match(/\b(\d+\.\d+\.\d+\.\d+)\b/)?.[1]
  : null;
if (channel === 'cft' && observedBinaryBuild !== build) {
  throw new Error(`CfT pin mismatch: expected ${build}, executable reports ${observedBinaryBuild}.`);
}

const launchOptions = {
  ...(channel === 'cft' ? { executablePath: cftExecutable } : { channel }),
  ...(browserSettings.fontconfig_file ? { env: { FONTCONFIG_FILE: browserSettings.fontconfig_file } } : {}),
  args: [
    '--headless=new',
    '--no-sandbox',
    '--disable-setuid-sandbox',
    '--disable-dev-shm-usage',
    `--window-size=${viewportWidth},${viewportHeight}`,
  ],
};

const sharedUse = {
  baseURL,
  headless: process.env.PLAYWRIGHT_HEADFUL ? false : true,
  viewport: { width: viewportWidth, height: viewportHeight },
  deviceScaleFactor,
  colorScheme,
  launchOptions,
  actionTimeout,
  navigationTimeout,
  trace: 'off',
  video: 'off',
  screenshot: {
    mode: 'only-on-failure',
    animations: 'disabled',
    caret: 'hide',
    fullPage: false,
  },
  reducedMotion: 'reduce',
};

export default defineConfig({
  testDir: process.env.PLAYWRIGHT_TEST_DIR ?? 'playwright',
  timeout: parseIntEnv('PLAYWRIGHT_TEST_TIMEOUT_MS', 60_000),
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: process.env.CI ? 2 : undefined,
  reporter: [['list']],
  outputDir: process.env.PLAYWRIGHT_OUTPUT_DIR ?? 'test-results',
  preserveOutput: 'always',
  expect: {
    toHaveScreenshot: {
      animations: 'disabled',
      caret: 'hide',
      fullPage: false,
    },
  },
  metadata: {
    viewport: `${viewportWidth}x${viewportHeight}@${deviceScaleFactor}x`,
    colorScheme,
    playwrightChannel: channel,
    cftExpectedBuild: build,
    cftObservedBinaryBuild: observedBinaryBuild,
    playwrightVersion,
    cftLabel: browserSettings.cft_label,
    cftExecutable: channel === 'cft' ? cftExecutable : null,
    playwrightTransport: 'cdp',
    requestedTransports: transportCandidates,
    screenshotStyleHash: browserSettings.screenshot_style_hash,
    screenshotMaskSelectors: maskSelectors,
  },
  use: sharedUse,
  projects: [
    {
      name: channel,
      use: sharedUse,
    },
  ],
});
