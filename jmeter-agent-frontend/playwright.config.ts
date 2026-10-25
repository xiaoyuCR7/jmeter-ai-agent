import { defineConfig } from '@playwright/test'

const port = 4173

export default defineConfig({
  testDir: 'e2e',
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  use: {
    baseURL: `http://localhost:${port}`,
    // CI installs Playwright's Chromium; locally PW_CHANNEL=chrome (or msedge) reuses an installed browser.
    channel: process.env.PW_CHANNEL || undefined,
    trace: 'retain-on-failure',
  },
  // A production build: the dev server reloads the page when it first optimizes a lazy route's dependencies.
  webServer: {
    command: `npx vite build && npx vite preview --port ${port} --strictPort`,
    timeout: 180_000,
    url: `http://localhost:${port}`,
    reuseExistingServer: !process.env.CI,
  },
})
