import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: './e2e', testMatch: 'peer.spec.ts', timeout: 60000, workers: 1,
  use: { baseURL: 'http://127.0.0.1:19749', headless: true,
    launchOptions: { executablePath: process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' } },
  webServer: { command: '../../.venv/bin/python e2e/backend.py', env: { COMPASS_PEER_E2E: '1' },
    url: 'http://127.0.0.1:19749/api/mobile/status', timeout: 30000, reuseExistingServer: false },
});
