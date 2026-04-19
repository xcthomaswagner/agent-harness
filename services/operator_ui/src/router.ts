/**
 * Tiny path-based router.
 *
 * The SPA is served from ``/operator`` — the router strips that prefix and
 * works against the remainder. Listens to ``popstate`` and intercepts
 * ``<a>`` clicks so ``navigate()`` updates history without a full reload.
 *
 * No external dependency: we have 7 routes, all client-side, no search or
 * query patterns beyond simple ``:param`` segments.
 */

import { useEffect, useState } from "preact/hooks";

const BASE = "/operator";

export type Route =
  | { name: "home" }
  | { name: "tickets" }
  | { name: "traces" }
  | { name: "trace-detail"; id: string }
  | { name: "autonomy"; profile?: string }
  | { name: "learning" }
  | { name: "pr-detail"; id: string }
  | { name: "not-found" };

function stripBase(pathname: string): string {
  if (pathname === BASE) return "/";
  if (pathname.startsWith(BASE + "/")) {
    return pathname.slice(BASE.length) || "/";
  }
  return pathname;
}

function joinBase(path: string): string {
  if (path === "/") return BASE + "/";
  return BASE + (path.startsWith("/") ? path : "/" + path);
}

export function parseRoute(path: string): Route {
  const p = stripBase(path).replace(/\/+$/, "") || "/";
  if (p === "/") return { name: "home" };
  if (p === "/tickets") return { name: "tickets" };
  if (p === "/traces") return { name: "traces" };
  if (p === "/learning") return { name: "learning" };

  const traceMatch = /^\/traces\/([^/]+)$/.exec(p);
  if (traceMatch) {
    return { name: "trace-detail", id: decodeURIComponent(traceMatch[1]!) };
  }

  const prMatch = /^\/pr\/([^/]+)$/.exec(p);
  if (prMatch) {
    return { name: "pr-detail", id: decodeURIComponent(prMatch[1]!) };
  }

  const autoMatch = /^\/autonomy(?:\/([^/]+))?$/.exec(p);
  if (autoMatch) {
    const profile = autoMatch[1];
    return profile
      ? { name: "autonomy", profile: decodeURIComponent(profile) }
      : { name: "autonomy" };
  }

  return { name: "not-found" };
}

export function navigate(path: string): void {
  const full = joinBase(path);
  if (typeof window === "undefined") return;
  if (window.location.pathname === full) return;
  window.history.pushState({}, "", full);
  window.dispatchEvent(new PopStateEvent("popstate"));
}

export function useRoute(): Route {
  const initial = typeof window === "undefined" ? "/" : window.location.pathname;
  const [route, setRoute] = useState<Route>(parseRoute(initial));

  useEffect(() => {
    const onPop = () => setRoute(parseRoute(window.location.pathname));
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  return route;
}

/**
 * Global click interceptor: every same-origin /operator link click
 * turns into history.pushState + a popstate event so every useRoute()
 * hook re-reads.
 *
 * Installed ONCE at app boot (see main.tsx). Doing this inside
 * useRoute caused subtle bugs: only the instance that handled the
 * click dispatched a popstate, so sibling components (App vs.
 * Sidebar) disagreed about which route was active after a click.
 */
export function installGlobalLinkInterceptor(): () => void {
  if (typeof window === "undefined") return () => {};
  const onClick = (e: MouseEvent) => {
    if (e.defaultPrevented) return;
    if (e.button !== 0) return;
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    const target = (e.target as HTMLElement | null)?.closest("a");
    if (!target || target.target === "_blank") return;
    const href = target.getAttribute("href") ?? "";
    if (!href.startsWith(BASE)) return;
    e.preventDefault();
    if (window.location.pathname === href) return;
    window.history.pushState({}, "", href);
    window.dispatchEvent(new PopStateEvent("popstate"));
  };
  document.addEventListener("click", onClick);
  return () => document.removeEventListener("click", onClick);
}

/**
 * Build a URL for the given route that clients can use in ``<a href>``.
 *
 * Keeping this co-located with parseRoute gives us one file to update when
 * adding a route.
 */
export function href(route: Route): string {
  switch (route.name) {
    case "home":
      return joinBase("/");
    case "tickets":
      return joinBase("/tickets");
    case "traces":
      return joinBase("/traces");
    case "trace-detail":
      return joinBase(`/traces/${encodeURIComponent(route.id)}`);
    case "autonomy":
      return joinBase(
        route.profile
          ? `/autonomy/${encodeURIComponent(route.profile)}`
          : "/autonomy",
      );
    case "learning":
      return joinBase("/learning");
    case "pr-detail":
      return joinBase(`/pr/${encodeURIComponent(route.id)}`);
    default:
      return joinBase("/");
  }
}
