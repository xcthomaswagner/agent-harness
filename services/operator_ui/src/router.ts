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

    // Intercept in-app link clicks so same-origin /operator links don't
    // trigger a full reload. External links + new-tab clicks (meta/ctrl)
    // still navigate normally.
    const onClick = (e: MouseEvent) => {
      if (e.defaultPrevented) return;
      if (e.button !== 0) return;
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      const target = (e.target as HTMLElement | null)?.closest("a");
      if (!target || target.target === "_blank") return;
      const href = target.getAttribute("href") ?? "";
      if (!href) return;
      if (href.startsWith(BASE)) {
        e.preventDefault();
        window.history.pushState({}, "", href);
        onPop();
      }
    };
    document.addEventListener("click", onClick);

    return () => {
      window.removeEventListener("popstate", onPop);
      document.removeEventListener("click", onClick);
    };
  }, []);

  return route;
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
