import { useEffect, useState } from "preact/hooks";
import { getStoredTheme, setStoredTheme } from "../theme";
import type { Route } from "../router";

interface TopbarProps {
  route: Route;
}

/**
 * Topbar: breadcrumb (derived from route) + live indicator + theme toggle.
 *
 * The live indicator currently shows a static "OK" pulse because the SPA
 * doesn't have an aggregate-health SSE stream yet. Commit 11's live log
 * will populate per-ticket status; later work can add a global health
 * channel. Keep the indicator so the visual design lands now and the
 * wiring slots in later.
 */
export function Topbar({ route }: TopbarProps) {
  const [theme, setTheme] = useState(getStoredTheme());

  useEffect(() => {
    setStoredTheme(theme);
  }, [theme]);

  return (
    <header class="op-topbar">
      <Breadcrumb route={route} />
      <div class="op-topbar-spacer" />
      <div class="op-topbar-live">
        <span class="op-topbar-live-dot" aria-hidden="true" />
        <span>Live</span>
      </div>
      <button
        type="button"
        class="op-theme-toggle"
        aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
        onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
      />
    </header>
  );
}

function Breadcrumb({ route }: { route: Route }) {
  const trail = crumbFor(route);
  return (
    <div class="op-crumb">
      {trail.map((part, i) => (
        <span key={i}>
          {i > 0 && <span class="op-topbar-sep"> · </span>}
          {part.strong ? <b>{part.label}</b> : part.label}
        </span>
      ))}
    </div>
  );
}

interface Crumb {
  label: string;
  strong?: boolean;
}

function crumbFor(route: Route): Crumb[] {
  switch (route.name) {
    case "home":
      return [{ label: "Overview" }, { label: "Home", strong: true }];
    case "tickets":
      return [{ label: "Pipeline" }, { label: "Tickets", strong: true }];
    case "traces":
      return [{ label: "Pipeline" }, { label: "Traces", strong: true }];
    case "trace-detail":
      return [
        { label: "Pipeline" },
        { label: "Traces" },
        { label: route.id, strong: true },
      ];
    case "autonomy":
      return [
        { label: "Ops" },
        {
          label: route.profile ? `Autonomy · ${route.profile}` : "Autonomy",
          strong: true,
        },
      ];
    case "learning":
      return [{ label: "Ops" }, { label: "Lessons", strong: true }];
    case "pr-detail":
      return [
        { label: "Pipeline" },
        { label: "PR" },
        { label: route.id, strong: true },
      ];
    default:
      return [{ label: "Not found", strong: true }];
  }
}
