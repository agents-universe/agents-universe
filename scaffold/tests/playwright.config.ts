import { defineConfig, devices } from '@playwright/test';

// Run shape is a tunable, not policy. The knobs exist because the same config
// has to serve a laptop, a shared test environment and a CI box:
//   PW_SERIAL=1    run one case at a time (cases that share mutable state)
//   PW_WORKERS=2   cap browser processes (small boxes, shared environments)
//   PW_RETRIES=1   retry a failure (a known-flaky target; off by default)
const serial = process.env.PW_SERIAL === '1';

export default defineConfig({
  testDir: './generated',
  timeout: 60_000,
  // Retries off by default: a retry doubles the wall clock of every failing
  // case, and the retried attempt overwrites the first one's video/trace — the
  // evidence is then of a different run than the failure that was reported.
  retries: process.env.PW_RETRIES ? Number(process.env.PW_RETRIES) : 0,
  // Cases inside one spec file run in parallel — a card whose cases each set up
  // their own data pays for one browser at a time otherwise. Cases that depend
  // on each other's effects must be merged or wrapped in
  // `test.describe.configure({ mode: 'serial' })`; PW_SERIAL=1 turns the whole
  // run serial without editing a spec.
  fullyParallel: !serial,
  workers: serial ? 1 : undefined,
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL: process.env.APP_BASE_URL || 'http://localhost:3000',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    trace: 'retain-on-failure',
    acceptDownloads: true,
  },
  projects: [
    {
      // Signs in once per run and hands the session to the chromium project, so
      // a spec no longer logs in in every case. Runs even when a single spec
      // file is selected (`playwright test generated/x.spec.ts`): Playwright
      // keeps a selected project's dependencies.
      name: 'setup',
      testDir: '.',
      testMatch: /auth\.setup\.ts/,
    },
    {
      name: 'chromium',
      dependencies: ['setup'],
      testMatch: /\.spec\.ts/,
      use: {
        ...devices['Desktop Chrome'],
        // Must stay the path auth.setup.ts writes. A case that must start
        // signed out (sign-in, role/permission, sign-out) opts out with
        // `test.use({ storageState: { cookies: [], origins: [] } })`.
        storageState: '.auth/state.json',
      },
    },
  ],
  outputDir: './test-results',
});
