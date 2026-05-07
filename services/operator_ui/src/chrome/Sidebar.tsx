import { BrandGlyph, Wordmark } from "../primitives";
import type { Route } from "../router";
import { href, useRoute } from "../router";

interface NavItem {
  label: string;
  target: Route;
  /** Optional count badge. */
  count?: number;
}

interface NavGroup {
  label: string;
  items: NavItem[];
}

const NAV: readonly NavGroup[] = [
  {
    label: "Operate",
    items: [
      { label: "Command Center", target: { name: "home" } },
      { label: "Runs", target: { name: "runs" } },
    ],
  },
  {
    label: "Improve",
    items: [
      { label: "Client Health", target: { name: "autonomy" } },
      { label: "Learning", target: { name: "learning" } },
    ],
  },
  {
    label: "Setup",
    items: [
      { label: "Repo Workflow", target: { name: "repo-workflow" } },
    ],
  },
];

function isActive(current: Route, target: Route): boolean {
  if (current.name === target.name) return true;
  // Run detail nests under "Runs"; PR detail doesn't match any sidebar
  // entry because it's reached from Traces or PR links, not direct nav.
  if (
    target.name === "runs" &&
    (current.name === "trace-detail" ||
      current.name === "tickets" ||
      current.name === "traces")
  ) {
    return true;
  }
  return false;
}

export function Sidebar() {
  const route = useRoute();
  return (
    <aside class="op-side">
      <div class="op-side-brand">
        <BrandGlyph />
        <Wordmark />
      </div>

      {NAV.map((group) => (
        <div class="op-nav-group" key={group.label}>
          <div class="op-nav-group-hd">{group.label}</div>
          {group.items.map((item) => (
            <a
              key={item.label}
              class={`op-nav-item${isActive(route, item.target) ? " is-active" : ""}`}
              href={href(item.target)}
            >
              <span>{item.label}</span>
              {typeof item.count === "number" && (
                <span class="op-nav-count">{item.count}</span>
              )}
            </a>
          ))}
        </div>
      ))}

      <div class="op-side-spacer" />
      <div class="op-side-who">
        <b>Operator</b>
        <span>Harness · ops</span>
      </div>
    </aside>
  );
}
