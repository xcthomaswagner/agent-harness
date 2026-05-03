/**
 * User preferences: theme, accent color, density.
 *
 * All three are applied to <html> as attributes / CSS custom properties
 * so every styled element resolves against the operator's chosen palette
 * without re-rendering the Preact tree. Persisted to localStorage.
 *
 * Applied synchronously BEFORE the Preact tree mounts (main.tsx calls
 * applyStoredPrefs()) so there's no flash-of-default-theme.
 */

export type Theme = "dark" | "light";
export type Density = "comfortable" | "cozy";

const KEY_THEME = "operator.theme";
const KEY_ACCENT = "operator.accent";
const KEY_DENSITY = "operator.density";

/**
 * Accent swatches the settings picker offers. Each is a {dark, light}
 * pair because the prototype's single "accent" token differs by theme
 * (amber in dark, darker orange in light). Custom hex colors apply the
 * same value to both themes — user's pick wins over theme-specific
 * contrast tuning.
 */
export interface AccentPreset {
  id: string;
  label: string;
  dark: string;
  light: string;
}

export const ACCENT_PRESETS: readonly AccentPreset[] = [
  { id: "amber", label: "Amber", dark: "#ff7a1a", light: "#d9530a" },
  { id: "teal", label: "Teal", dark: "#4ac7b8", light: "#208f7f" },
  { id: "violet", label: "Violet", dark: "#a374e0", light: "#7a3fd6" },
  { id: "indigo", label: "Indigo", dark: "#6a8df0", light: "#3f5cc4" },
  { id: "rose", label: "Rose", dark: "#e56a8f", light: "#b8345b" },
  { id: "emerald", label: "Emerald", dark: "#58c47e", light: "#2f8a50" },
];

/** Identifier stored in localStorage. May be a preset id or a hex string. */
export type AccentValue = string;

function readStored(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeStored(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* ignore — embedded previews may disable storage */
  }
}

// ---------- Theme ----------

export function getStoredTheme(): Theme {
  const raw = readStored(KEY_THEME);
  return raw === "light" || raw === "dark" ? raw : "dark";
}

export function setStoredTheme(t: Theme): void {
  document.documentElement.setAttribute("data-theme", t);
  writeStored(KEY_THEME, t);
  // Reapply accent so the theme-appropriate side of a preset kicks in.
  applyAccent(getStoredAccent(), t);
}

// ---------- Accent ----------

export function getStoredAccent(): AccentValue {
  return readStored(KEY_ACCENT) || "amber";
}

export function setStoredAccent(value: AccentValue): void {
  writeStored(KEY_ACCENT, value);
  applyAccent(value, getStoredTheme());
}

/**
 * Resolve a stored value to a concrete hex for the current theme.
 * Preset ids → look up in ACCENT_PRESETS. Raw hex → use as-is for
 * both themes. Unknown values → fall back to the amber preset.
 */
export function resolveAccent(value: AccentValue, theme: Theme): string {
  if (value.startsWith("#")) return value;
  const preset = ACCENT_PRESETS.find((p) => p.id === value);
  if (preset) return theme === "light" ? preset.light : preset.dark;
  const fallback = ACCENT_PRESETS[0]!;
  return theme === "light" ? fallback.light : fallback.dark;
}

function applyAccent(value: AccentValue, theme: Theme): void {
  const hex = resolveAccent(value, theme);
  document.documentElement.style.setProperty("--accent", hex);
  // Signal-active drives pills + phase-dot glow; keep it in sync so
  // "active/running" UI surfaces always pick up the operator's choice.
  document.documentElement.style.setProperty("--signal-active", hex);
}

// ---------- Density ----------

export function getStoredDensity(): Density {
  const raw = readStored(KEY_DENSITY);
  return raw === "cozy" ? "cozy" : "comfortable";
}

export function setStoredDensity(d: Density): void {
  if (d === "cozy") {
    document.documentElement.setAttribute("data-density", "cozy");
  } else {
    document.documentElement.removeAttribute("data-density");
  }
  writeStored(KEY_DENSITY, d);
}

// ---------- Boot ----------

export function applyStoredPrefs(): void {
  const theme = getStoredTheme();
  document.documentElement.setAttribute("data-theme", theme);
  applyAccent(getStoredAccent(), theme);
  const density = getStoredDensity();
  if (density === "cozy") {
    document.documentElement.setAttribute("data-density", "cozy");
  }
}
