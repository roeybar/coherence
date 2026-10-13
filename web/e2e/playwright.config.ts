import { defineConfig } from '@playwright/test';

const baseURL = process.env.BASE_URL || 'http://localhost:8080';

// One worker: the specs share one engine and its rate limits.
export default defineConfig({
  testDir: './specs',
  timeout: 120_000,
  expect: { timeout: 15_000 },
  workers: 1,
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL,
    viewport: { width: 440, height: 900 },
    // the typewriter jumps straight to the full text
    reducedMotion: 'reduce',
    // keep a trace of a failing test only
    trace: 'retain-on-failure',
    // the test network is plain http; WebCrypto needs a secure origin, and only the
    // full browser (not the headless shell) honours the flag below
    channel: 'chromium',
    launchOptions: { args: [`--unsafely-treat-insecure-origin-as-secure=${new URL(baseURL).origin}`] },
  },
});
