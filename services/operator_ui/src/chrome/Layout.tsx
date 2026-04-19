import type { ComponentChildren } from "preact";
import { Sidebar } from "./Sidebar";
import { Topbar } from "./Topbar";
import type { Route } from "../router";
import "./chrome.css";

interface LayoutProps {
  route: Route;
  children: ComponentChildren;
}

export function Layout({ route, children }: LayoutProps) {
  return (
    <div class="op-app">
      <Sidebar />
      <main class="op-main">
        <Topbar route={route} />
        <div class="op-content">{children}</div>
      </main>
    </div>
  );
}
