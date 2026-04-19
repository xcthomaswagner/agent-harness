/**
 * Theme persistence — dark (default) or light.
 *
 * Applied to <html data-theme="..."> so every CSS variable resolves against
 * the correct palette. Persists in localStorage under OPERATOR_THEME_KEY.
 */

export type Theme = "dark" | "light";

const OPERATOR_THEME_KEY = "operator.theme";

export function getStoredTheme(): Theme {
  try {
    const raw = localStorage.getItem(OPERATOR_THEME_KEY);
    if (raw === "light" || raw === "dark") return raw;
  } catch {
    /* localStorage may be disabled in the embedded preview; fall through. */
  }
  return "dark";
}

export function setStoredTheme(t: Theme): void {
  document.documentElement.setAttribute("data-theme", t);
  try {
    localStorage.setItem(OPERATOR_THEME_KEY, t);
  } catch {
    /* ignore */
  }
}

export function applyStoredTheme(): void {
  setStoredTheme(getStoredTheme());
}
