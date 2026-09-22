import { test } from '@playwright/test';
import fs from 'fs';
import path from 'path';

// Playwright resolves a relative `storageState` path (see playwright.config.ts)
// and this script's writes against the working directory, which is `tests/`
// for every run the platform starts. Keep the two spellings in step if either
// side ever moves.
const STATE_PATH = '.auth/state.json';
const EMPTY_STATE = JSON.stringify({ cookies: [], origins: [] });

test('authenticate', async ({ page }) => {
  fs.mkdirSync(path.dirname(STATE_PATH), { recursive: true });

  const baseUrl = process.env.APP_BASE_URL || 'http://localhost:3000';
  const loginUrl = process.env.APP_LOGIN_URL || `${baseUrl}/login`;
  const username = process.env.APP_USERNAME || '';
  const password = process.env.APP_PASSWORD || '';

  if (!username || !password) {
    // Unauthenticated coverage is a legitimate run, so this is a skip rather
    // than a failure — but the empty state file is written first, because a
    // skip that left the file absent would fail every case that names it.
    fs.writeFileSync(STATE_PATH, EMPTY_STATE);
    test.skip(true, 'APP_USERNAME/APP_PASSWORD not set - running without a session');
    return;
  }

  await page.goto(loginUrl, { waitUntil: 'domcontentloaded' });
  await page.getByLabel(/user|email|account/i).fill(username);
  await page.getByLabel(/pass/i).fill(password);
  await page.getByRole('button', { name: /log|sign|submit/i }).click();

  // Deliberately no `.catch()`: a sign-in that quietly gave up would surface as
  // one confusing failure per case instead of one clear failure here, and it
  // would be attempted once per case rather than once per run. "Left the login
  // page" is the assertion because the post-login route differs per app.
  await page.waitForURL((url) => !/\/login\b/i.test(url.pathname), { timeout: 30_000 });

  await page.context().storageState({ path: STATE_PATH });
});
