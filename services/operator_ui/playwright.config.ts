import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright runs against the live L1 on localhost:8000 with the real
 * API key from services/l1_preprocessing/.env. This exercises the SPA
 * against real data, which is exactly the assurance that broke last
 * time (tests-in-isolation passed, the browser didn't render).
 *
 * Run from services/operator_ui/:
 *   npx playwright test
 *   npx playwright test --ui           # interactive debugger
 *   npx playwright test --update-snapshots
 */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false, // Single L1 instance; parallelism pointless.
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: "http://localhost:8000",
    viewport: { width: 1440, height: 900 },
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
