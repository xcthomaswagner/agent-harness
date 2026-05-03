import { useEffect, useRef, useState } from "preact/hooks";
import { fetchHeaders } from "../api/key";
import type { ModelPolicyResponse, ModelPolicyRole } from "../api/types";
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
  const [policy, setPolicy] = useState<ModelPolicyResponse | null>(null);
  const [policyState, setPolicyState] = useState<"loading" | "ok" | "saving" | "error">(
    "loading",
  );
  const [policyError, setPolicyError] = useState<string>("");
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

  useEffect(() => {
    let cancelled = false;
    setPolicyState("loading");
    fetch("/api/operator/model-policy", {
      headers: fetchHeaders({ Accept: "application/json" }),
      credentials: "same-origin",
    })
      .then(async (res) => {
        if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
        return (await res.json()) as ModelPolicyResponse;
      })
      .then((data) => {
        if (cancelled) return;
        setPolicy(data);
        setPolicyState("ok");
        setPolicyError("");
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setPolicyState("error");
        setPolicyError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, []);

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

  const updatePolicyRole = (
    role: string,
    patch: Partial<Pick<ModelPolicyRole, "model" | "reasoning">>,
  ) => {
    if (!policy) return;
    setPolicy({
      ...policy,
      roles: policy.roles.map((r) => (r.role === role ? { ...r, ...patch } : r)),
    });
  };

  const savePolicy = () => {
    if (!policy) return;
    setPolicyState("saving");
    fetch("/api/operator/model-policy", {
      method: "PUT",
      headers: fetchHeaders({
        Accept: "application/json",
        "Content-Type": "application/json",
      }),
      credentials: "same-origin",
      body: JSON.stringify({
        roles: policy.roles.map((r) => ({
          role: r.role,
          model: r.model,
          reasoning: r.reasoning,
        })),
      }),
    })
      .then(async (res) => {
        if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
        return (await res.json()) as ModelPolicyResponse;
      })
      .then((data) => {
        setPolicy(data);
        setPolicyState("ok");
        setPolicyError("");
      })
      .catch((err: unknown) => {
        setPolicyState("error");
        setPolicyError(err instanceof Error ? err.message : String(err));
      });
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

      <div class="op-settings-section">
        <div class="op-settings-label">Models</div>
        {policyState === "loading" && (
          <div class="op-settings-note">Loading model policy...</div>
        )}
        {policyState === "error" && (
          <div class="op-settings-error">Model policy unavailable: {policyError}</div>
        )}
        {policy && (
          <>
            <div class="op-model-grid">
              {policy.roles.map((role) => (
                <div class="op-model-row" key={role.role}>
                  <span class="op-model-role">{role.label}</span>
                  <select
                    value={role.model}
                    onChange={(e) =>
                      updatePolicyRole(role.role, {
                        model: (e.target as HTMLSelectElement).value,
                      })
                    }
                  >
                    {policy.model_options.map((model) => (
                      <option key={model} value={model}>
                        {model}
                      </option>
                    ))}
                  </select>
                  <select
                    value={role.reasoning}
                    onChange={(e) =>
                      updatePolicyRole(role.role, {
                        reasoning: (e.target as HTMLSelectElement).value,
                      })
                    }
                  >
                    {policy.reasoning_options.map((reasoning) => (
                      <option key={reasoning} value={reasoning}>
                        {reasoning}
                      </option>
                    ))}
                  </select>
                </div>
              ))}
            </div>
            <div class="op-settings-actions">
              <span class="op-settings-note">
                {policy.source === "local" ? "Local policy" : "Default policy"}
              </span>
              <button
                type="button"
                class="op-settings-save"
                disabled={policyState === "saving"}
                onClick={savePolicy}
              >
                {policyState === "saving" ? "Saving" : "Save"}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
