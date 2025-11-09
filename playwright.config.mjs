import { defineConfig } from '@playwright/test';

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

const viewportWidth = parseIntEnv('PLAYWRIGHT_VIEWPORT_WIDTH', 1280);
const viewportHeight = parseIntEnv('PLAYWRIGHT_VIEWPORT_HEIGHT', 2000);
const deviceScaleFactor = parseIntEnv('PLAYWRIGHT_DEVICE_SCALE_FACTOR', 2);
const actionTimeout = parseIntEnv('PLAYWRIGHT_ACTION_TIMEOUT_MS', 15_000);
const navigationTimeout = parseIntEnv('PLAYWRIGHT_NAVIGATION_TIMEOUT_MS', 30_000);
const colorScheme = process.env.PLAYWRIGHT_COLOR_SCHEME?.toLowerCase() === 'dark' ? 'dark' : 'light';
const baseURL = process.env.PLAYWRIGHT_BASE_URL ?? 'http://localhost:8000';
const channelCandidates = parseListEnv(process.env.PLAYWRIGHT_CHANNEL, 'cft,chromium');
const channel = channelCandidates[0];
const channelFallbacks = channelCandidates.slice(1);

const transportCandidates = parseListEnv(process.env.PLAYWRIGHT_TRANSPORT, 'cdp');
const resolvedTransport = (transportCandidates[0] ?? 'cdp').toLowerCase();
const transportFallbacks = transportCandidates.slice(1).map((entry) => entry.toLowerCase());
const screenshotPath = process.env.PLAYWRIGHT_SCREENSHOT_DIR ?? undefined;
const maskSelectors = (process.env.PLAYWRIGHT_SCREENSHOT_MASKS ?? '')
  .split(',')
  .map((value) => value.trim())
  .filter(Boolean);

const launchOptions = {
  channel,
};

if (resolvedTransport === 'bidi') {
  // @ts-ignore -- protocol is a documented experimental option in Playwright 1.50+
  launchOptions.protocol = 'webDriverBiDi';
}

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
    fullPage: true,
    path: screenshotPath,
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
  metadata: {
    viewport: `${viewportWidth}x${viewportHeight}@${deviceScaleFactor}x`,
    colorScheme,
    playwrightChannel: channel,
    playwrightChannelFallbacks: channelFallbacks,
    playwrightTransport: resolvedTransport === 'bidi' ? 'webDriverBiDi' : 'cdp',
    playwrightTransportFallbacks: transportFallbacks,
  },
  use: sharedUse,
  projects: [
    {
      name: 'chromium',
      use: {
        ...sharedUse,
        mask: maskSelectors,
      },
    },
  ],
});
