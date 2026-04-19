import { Layout, ViewHead } from "./chrome";
import { useRoute } from "./router";
import type { Route } from "./router";
import { HomeView } from "./views";

/**
 * Top-level app. Layout renders the sidebar + topbar; the route decides
 * which view renders in the content slot. Real view components land in
 * subsequent commits (Home, Traces, Trace Detail, Autonomy, Learning,
 * PR, Tickets) — for now each shows a placeholder so the router can be
 * exercised end-to-end.
 */
export function App() {
  const route = useRoute();
  return (
    <Layout route={route}>
      <ViewFor route={route} />
    </Layout>
  );
}

function ViewFor({ route }: { route: Route }) {
  switch (route.name) {
    case "home":
      return <HomeView />;
    case "tickets":
      return (
        <ViewHead
          sup="Pipeline · tickets"
          title="Tickets"
          sub="Live pipeline board."
        />
      );
    case "traces":
      return (
        <ViewHead
          sup="Pipeline · traces"
          title="Traces"
          sub="Every run across every profile."
        />
      );
    case "trace-detail":
      return (
        <ViewHead
          sup={`Traces · ${route.id}`}
          title={route.id}
          sub="Phase timeline · session panels · raw events."
        />
      );
    case "autonomy":
      return (
        <ViewHead
          sup="Ops · autonomy"
          title={
            route.profile
              ? `Autonomy report — ${route.profile}`
              : "Autonomy report"
          }
          sub="First-pass accept, escape rate, auto-merge adoption."
        />
      );
    case "learning":
      return (
        <ViewHead
          sup="Ops · learning"
          title="Lessons"
          sub="Proposed → applied triage queue."
        />
      );
    case "pr-detail":
      return (
        <ViewHead
          sup={`PR · ${route.id}`}
          title={route.id}
          sub="Checks · issues · lesson matches · auto-merge decision."
        />
      );
    default:
      return (
        <ViewHead
          sup="Operator"
          title="Not found"
          sub="The page you asked for does not exist."
        />
      );
  }
}
