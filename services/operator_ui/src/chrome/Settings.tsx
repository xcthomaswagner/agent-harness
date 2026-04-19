import { useEffect, useRef, useState } from "preact/hooks";
import {
  ACCENT_PRESETS,
  getStoredAccent,
  getStoredDensity,
  getStoredTheme,
  setStoredAccent,
  setStoredDensity,
  setStoredTheme,
} from "../theme";
import type { AccentPreset, Density, Theme } from "../theme";

/**
 * Settings popover — opens from the topbar gear button. Three controls:
 *   - Theme (dark / light)
 *   - Accent color (6 preset swatches + custom hex)
 *   - Density (comfortable / cozy)
 *
 * All values persist to localStorage via ../theme.ts helpers. CSS custom
 * properties update synchronously so the UI reflects the change without
 * a re-render round-trip.
 */
export function Settings({ onClose }: { onClose: () => void }) {
  const [theme, setTheme] = useState<Theme>(getStoredTheme());
  const [accent, setAccent] = useState<string>(getStoredAccent());
  const [density, setDensity] = useState<Density>(getStoredDensity());
  const panelRef = useRef<HTMLDivElement | null>(null);

  // Dismiss on outside click or Escape.
  useEffect(() => {
    const onDoc = (e: MouseEvent) => {
      if (!panelRef.current) return;
      if (!panelRef.current.contains(e.target as Node)) onClose();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [onClose]);

  const pickTheme = (t: Theme) => {
    setTheme(t);
    setStoredTheme(t);
  };

  const pickAccent = (value: string) => {
    setAccent(value);
    setStoredAccent(value);
  };

  const pickDensity = (d: Density) => {
    setDensity(d);
    setStoredDensity(d);
  };

  const isHex = accent.startsWith("#");

  return (
    <div class="op-settings" ref={panelRef} role="dialog" aria-label="Preferences">
      <div class="op-settings-section">
        <div class="op-settings-label">Theme</div>
        <div class="op-settings-row">
          <button
            type="button"
            class={`op-settings-pill${theme === "dark" ? " is-on" : ""}`}
            onClick={() => pickTheme("dark")}
          >
            Dark
          </button>
          <button
            type="button"
            class={`op-settings-pill${theme === "light" ? " is-on" : ""}`}
            onClick={() => pickTheme("light")}
          >
            Light
          </button>
        </div>
      </div>

      <div class="op-settings-section">
        <div class="op-settings-label">Accent</div>
        <div class="op-settings-swatches">
          {ACCENT_PRESETS.map((p: AccentPreset) => {
            const selected = accent === p.id;
            const swatch = theme === "light" ? p.light : p.dark;
            return (
              <button
                key={p.id}
                type="button"
                class={`op-settings-swatch${selected ? " is-on" : ""}`}
                title={p.label}
                aria-label={p.label}
                aria-pressed={selected}
                style={{ background: swatch }}
                onClick={() => pickAccent(p.id)}
              />
            );
          })}
        </div>
        <label class="op-settings-hex">
          <span>Custom</span>
          <input
            type="text"
            value={isHex ? accent : ""}
            placeholder="#RRGGBB"
            maxLength={7}
            pattern="^#[0-9A-Fa-f]{6}$"
            onInput={(e) => {
              const v = (e.target as HTMLInputElement).value.trim();
              if (/^#[0-9A-Fa-f]{6}$/.test(v)) pickAccent(v);
            }}
          />
        </label>
      </div>

      <div class="op-settings-section">
        <div class="op-settings-label">Density</div>
        <div class="op-settings-row">
          <button
            type="button"
            class={`op-settings-pill${density === "comfortable" ? " is-on" : ""}`}
            onClick={() => pickDensity("comfortable")}
          >
            Comfortable
          </button>
          <button
            type="button"
            class={`op-settings-pill${density === "cozy" ? " is-on" : ""}`}
            onClick={() => pickDensity("cozy")}
          >
            Cozy
          </button>
        </div>
      </div>
    </div>
  );
}
