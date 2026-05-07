import { Layout, ViewHead } from "./chrome";
import { useRoute } from "./router";
import type { Route } from "./router";
import {
  AutonomyView,
  HomeView,
  LearningView,
  PRDetailView,
  RepoWorkflowView,
  RunsView,
  TraceDetailView,
} from "./views";

/**
 * Top-level app. Layout renders the sidebar + topbar; the route decides
 * which view renders in the content slot. Real view components land in
 * subsequent commits (Command Center, Runs, Trace Detail, Client Health,
 * Learning, PR, Setup) — route compatibility remains for older links.
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
    case "runs":
    case "tickets":
    case "traces":
      return <RunsView />;
    case "trace-detail":
      return <TraceDetailView id={route.id} />;
    case "autonomy":
      return <AutonomyView profile={route.profile} />;
    case "learning":
      return <LearningView />;
    case "repo-workflow":
      return <RepoWorkflowView />;
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
