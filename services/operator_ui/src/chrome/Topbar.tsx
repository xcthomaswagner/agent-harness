import { useState } from "preact/hooks";
import type { OperatorSystemResponse } from "../api/types";
import { useFeed } from "../hooks/useFeed";
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
  const system = useFeed<OperatorSystemResponse>("/api/operator/system", {
    intervalMs: 15_000,
  });
  const systemInfo = system.data;
  const systemLabel = systemInfo
    ? `L1 ${systemInfo.git_sha || systemInfo.version} · ${formatTime(systemInfo.started_at)}`
    : system.status === "error"
      ? "L1 unavailable"
      : "L1 checking";
  const systemTitle = systemInfo
    ? [
        `PID ${systemInfo.pid}`,
        `Started ${systemInfo.started_at}`,
        `Uptime ${formatUptime(systemInfo.uptime_seconds)}`,
        `Branch ${systemInfo.git_branch || "unknown"}`,
        `DB ${systemInfo.db_path}`,
        `Bundle ${systemInfo.operator_bundle.rev || "unknown"} ${systemInfo.operator_bundle.built_at || ""}`.trim(),
      ].join("\n")
    : system.error || "";

  return (
    <header class="op-topbar">
      <Breadcrumb route={route} />
      <div class="op-topbar-spacer" />
      <div
        class={`op-topbar-live${system.status === "error" ? " is-err" : ""}`}
        title={systemTitle}
      >
        <span class="op-topbar-live-dot" aria-hidden="true" />
        <span>{system.status === "error" ? "Stale" : "Live"}</span>
      </div>
      <div class="op-topbar-system" title={systemTitle}>
        {systemLabel}
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

function formatTime(value: string): string {
  if (!value) return "--:--";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "--:--";
  return date.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatUptime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "unknown";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
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
      return [{ label: "Operate" }, { label: "Command Center", strong: true }];
    case "runs":
      return [{ label: "Operate" }, { label: "Runs", strong: true }];
    case "tickets":
    case "traces":
      return [{ label: "Operate" }, { label: "Runs", strong: true }];
    case "trace-detail":
      return [
        { label: "Operate" },
        { label: "Runs" },
        { label: route.id, strong: true },
      ];
    case "autonomy":
      return [
        { label: "Improve" },
        {
          label: route.profile ? `Client Health · ${route.profile}` : "Client Health",
          strong: true,
        },
      ];
    case "learning":
      return [{ label: "Improve" }, { label: "Learning", strong: true }];
    case "repo-workflow":
      return [{ label: "Setup" }, { label: "Repo Workflow", strong: true }];
    case "pr-detail":
      return [
        { label: "Operate" },
        { label: "PR" },
        { label: route.id, strong: true },
      ];
    default:
      return [{ label: "Not found", strong: true }];
  }
}
