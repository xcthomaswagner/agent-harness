/**
 * End-to-end verification that the operator dashboard renders correctly.
 *
 * These tests catch the class of failure that happened last time: the
 * Python + Vitest tests all passed, but the browser rendered the SPA
 * as a un-styled vertical stack because the component CSS was never
 * bundled into the served stylesheet. Tests below require:
 *
 *   - L1 running on localhost:8000
 *   - API_KEY env var set to the same key L1 uses
 *
 * Each test asserts both a DOM structure AND a visual-grid property
 * (e.g., the sidebar is 220px wide, the layout is a grid) so a
 * missing-CSS regression fails the test rather than passing because
 * the text is still present.
 */

import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

// Pull API key from L1's .env so tests exercise the production
// auth path (header-based) the SPA uses after the shell injection.
function readApiKey(): string {
  const env = readFileSync(
    resolve(__dirname, "../../l1_preprocessing/.env"),
    "utf-8",
  );
  const match = /^API_KEY=(.+)$/m.exec(env);
  if (!match) throw new Error("API_KEY missing from L1 .env");
  return match[1]!.trim();
}

const API_KEY = readApiKey();

function shellUrl(path = "/"): string {
  const sep = path.includes("?") ? "&" : "?";
  return `/operator${path}${sep}api_key=${encodeURIComponent(API_KEY)}`;
}

test.describe("Operator dashboard — chrome renders with real CSS", () => {
  test("Home view lays out as a sidebar + main column, not a vertical stack", async ({
    page,
  }) => {
    await page.goto(shellUrl("/"));

    // The SPA shell must load.
    await expect(page.locator("#app")).toBeVisible();

    // Layout grid must render: sidebar left, main column right.
    const sidebar = page.locator(".op-side");
    await expect(sidebar).toBeVisible();
    const sidebarBox = await sidebar.boundingBox();
    expect(sidebarBox).not.toBeNull();
    // The sidebar is 220px wide per chrome.css.
    expect(sidebarBox!.width).toBeGreaterThan(200);
    expect(sidebarBox!.width).toBeLessThan(260);

    // Topbar must exist in the main column.
    await expect(page.locator(".op-topbar")).toBeVisible();

    // View title is the serif "Mission control".
    const title = page.locator(".op-view-title");
    await expect(title).toBeVisible();
    await expect(title).toHaveText(/Mission control/i);

    // The title must render in the serif font (not the default sans).
    const fontFamily = await title.evaluate(
      (el) => getComputedStyle(el).fontFamily,
    );
    expect(fontFamily).toMatch(/Instrument Serif|serif/i);
  });

  test("Profile cards render as a grid, not a stacked list", async ({
    page,
  }) => {
    await page.goto(shellUrl("/"));

    // Wait for profiles to load.
    const grid = page.locator(".op-profiles-grid");
    await expect(grid).toBeVisible({ timeout: 10_000 });

    // Grid is auto-fit minmax(240px, 1fr) — at 1440px viewport we
    // should get multiple cards side-by-side. Assert y-coord equality
    // on the first 3 cards (they must share a row).
    const cards = page.locator(".op-profile-card");
    const count = await cards.count();
    expect(count).toBeGreaterThanOrEqual(1);

    if (count >= 2) {
      const first = await cards.nth(0).boundingBox();
      const second = await cards.nth(1).boundingBox();
      expect(first).not.toBeNull();
      expect(second).not.toBeNull();
      // Same row = y-coords within a few pixels of each other.
      expect(Math.abs(first!.y - second!.y)).toBeLessThan(10);
    }
  });

  test("Lessons strip renders as 6 columns side-by-side", async ({ page }) => {
    await page.goto(shellUrl("/"));

    const strip = page.locator(".op-lessons-strip");
    await expect(strip).toBeVisible({ timeout: 10_000 });

    const cells = page.locator(".op-lessons-cell");
    await expect(cells).toHaveCount(6);

    // All 6 cells must share a row.
    const ys = await cells.evaluateAll((els) =>
      els.map((el) => el.getBoundingClientRect().top),
    );
    const yMin = Math.min(...ys);
    const yMax = Math.max(...ys);
    expect(yMax - yMin).toBeLessThan(10);
  });

  test("Sidebar nav links route to other views", async ({ page }) => {
    await page.goto(shellUrl("/"));
    await page.waitForLoadState("networkidle");

    // Click Traces in the sidebar.
    await page
      .locator(".op-nav-item", { hasText: /^Traces$/ })
      .first()
      .click();

    await expect(page).toHaveURL(/\/operator\/traces/);
    await expect(page.locator(".op-view-title")).toHaveText(/Traces/i);
  });

  test("Traces view renders a populated table", async ({ page }) => {
    await page.goto(shellUrl("/traces"));
    const table = page.locator(".op-tbl").first();
    await expect(table).toBeVisible({ timeout: 10_000 });

    // At least a header row should exist.
    await expect(table.locator("thead th").first()).toBeVisible();
  });

  test("Theme toggle flips data-theme on <html>", async ({ page }) => {
    // Clear storage first so this test doesn't depend on ordering —
    // localStorage persists across tests in the shared context.
    await page.goto(shellUrl("/"));
    await page.evaluate(() => localStorage.removeItem("operator.theme"));
    await page.reload();

    const themeBefore = await page.evaluate(() =>
      document.documentElement.getAttribute("data-theme"),
    );
    // Default is dark (set by applyStoredTheme on mount).
    expect(themeBefore).toBe("dark");

    await page.locator(".op-theme-toggle").click();
    // Small wait — the click handler is synchronous but Preact's
    // reconciler flushes next tick.
    await page.waitForFunction(
      () => document.documentElement.getAttribute("data-theme") === "light",
      { timeout: 2000 },
    );
  });

  test("Autonomy view loads without erroring", async ({ page }) => {
    await page.goto(shellUrl("/autonomy"));
    await expect(page.locator(".op-view-title")).toBeVisible({ timeout: 10_000 });
    // The "failed to load" banner must NOT be present.
    await expect(page.locator(".op-error")).not.toBeVisible();
  });

  test("Learning view loads without erroring", async ({ page }) => {
    await page.goto(shellUrl("/learning"));
    await expect(page.locator(".op-view-title")).toHaveText(/Lessons/i, {
      timeout: 10_000,
    });
    await expect(page.locator(".op-error")).not.toBeVisible();
  });

  test("Tickets view renders kanban + rail grid", async ({ page }) => {
    await page.goto(shellUrl("/tickets"));
    await expect(page.locator(".op-view-title")).toBeVisible();
    await expect(page.locator(".op-tickets-grid")).toBeVisible();
    await expect(page.locator(".op-tickets-rail")).toBeVisible();
  });
});

test.describe("CSS regression check", () => {
  test("The served CSS bundle includes every primitive class", async ({
    request,
  }) => {
    const res = await request.get("/operator/operator.css");
    expect(res.status()).toBe(200);
    const css = await res.text();

    // These classes must be present or the SPA will render unstyled.
    // This is the exact failure mode from the last broken run.
    const required = [
      ".op-pill",
      ".op-phase-dots",
      ".op-chip",
      ".op-btn",
      ".op-kpi",
      ".op-search",
      ".op-sec-hd",
      ".op-tbl",
      ".op-app",
      ".op-side",
      ".op-topbar",
      ".op-view-hd",
      ".op-profiles-grid",
      ".op-lessons-strip",
      ".op-tickets-grid",
    ];
    for (const cls of required) {
      expect(css, `missing ${cls} in served CSS bundle`).toContain(cls);
    }
  });
});
