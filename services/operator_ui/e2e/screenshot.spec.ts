/**
 * Screenshot-capture helper. Run:
 *   npx playwright test e2e/screenshot.spec.ts
 * Produces /tmp/operator-screens/<view>.png for manual inspection.
 */
import { test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const env = readFileSync(
  resolve(__dirname, "../../l1_preprocessing/.env"),
  "utf-8",
);
const API_KEY = /^API_KEY=(.+)$/m.exec(env)![1]!.trim();

const VIEWS: [string, string][] = [
  ["home", "/"],
  ["traces", "/traces"],
  ["autonomy", "/autonomy"],
  ["learning", "/learning"],
  ["tickets", "/tickets"],
];

for (const [name, path] of VIEWS) {
  test(`screenshot — ${name}`, async ({ page }) => {
    await page.goto(
      `/operator${path}?api_key=${encodeURIComponent(API_KEY)}`,
    );
    await page.waitForLoadState("networkidle");
    await page.waitForTimeout(600);
    await page.screenshot({
      path: `/tmp/operator-screens/${name}.png`,
      fullPage: false,
    });
  });
}
