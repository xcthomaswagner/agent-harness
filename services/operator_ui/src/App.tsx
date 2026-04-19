import { Layout, ViewHead } from "./chrome";
import { useRoute } from "./router";
import type { Route } from "./router";
import {
  AutonomyView,
  HomeView,
  LearningView,
  PRDetailView,
  TicketsView,
  TraceDetailView,
  TracesView,
} from "./views";

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
      return <TicketsView />;
    case "traces":
      return <TracesView />;
    case "trace-detail":
      return <TraceDetailView id={route.id} />;
    case "autonomy":
      return <AutonomyView profile={route.profile} />;
    case "learning":
      return <LearningView />;
    case "pr-detail":
      return <PRDetailView id={route.id} />;
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
