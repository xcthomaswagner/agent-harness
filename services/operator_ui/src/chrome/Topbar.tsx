import { useState } from "preact/hooks";
import type { Route } from "../router";
import { Settings } from "./Settings";

interface TopbarProps {
  route: Route;
}

/**
 * Topbar: breadcrumb (derived from route) + live indicator + settings
 * gear (theme / accent / density).
 *
 * The live indicator currently shows a static "OK" pulse because the
 * SPA doesn't have an aggregate-health SSE stream yet. Commit 11's
 * live log populates per-ticket status; a global health channel is a
 * followup.
 */
export function Topbar({ route }: TopbarProps) {
  const [settingsOpen, setSettingsOpen] = useState(false);

  return (
    <header class="op-topbar">
      <Breadcrumb route={route} />
      <div class="op-topbar-spacer" />
      <div class="op-topbar-live">
        <span class="op-topbar-live-dot" aria-hidden="true" />
        <span>Live</span>
      </div>
      <div class="op-settings-wrap">
        <button
          type="button"
          class="op-settings-btn"
          aria-label="Open preferences"
          aria-expanded={settingsOpen}
          onClick={() => setSettingsOpen((v) => !v)}
        >
          ⚙
        </button>
        {settingsOpen && <Settings onClose={() => setSettingsOpen(false)} />}
      </div>
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
