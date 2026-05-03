/**
 * SWR-style data hook.
 *
 * ``useFeed`` fetches a JSON endpoint with the operator API key attached,
 * refreshes on a fixed interval (default 10 s), and exposes a
 * stale-while-revalidate state machine:
 *
 *   status === "loading" && data === undefined  → first fetch
 *   status === "refreshing" && data !== undefined → background refresh
 *   status === "ok"                               → cached + fresh
 *   status === "error"                            → last fetch failed;
 *                                                    stale data retained
 *
 * Consumer renders against ``data``; the ``status`` drives indicator
 * badges only. This keeps views static during refreshes — no flicker.
 *
 * Note: this is intentionally minimal. If we ever need dependent fetches,
 * mutation, or global cache, swap for a real library (SWR or TanStack).
 */

import { useEffect, useRef, useState } from "preact/hooks";
import { fetchHeaders } from "../api/key";

export type FeedStatus = "idle" | "loading" | "refreshing" | "ok" | "error";

export interface FeedState<T> {
  data: T | undefined;
  status: FeedStatus;
  error: string | undefined;
  /** Trigger a manual refresh. */
  refresh: () => void;
}

interface UseFeedOptions {
  /** Refresh interval in ms. 0 disables polling. Default 10_000. */
  intervalMs?: number;
  /** Disable the hook entirely (e.g., when the view is collapsed). */
  disabled?: boolean;
}

export function useFeed<T>(
  url: string | null,
  opts: UseFeedOptions = {},
): FeedState<T> {
  const { intervalMs = 10_000, disabled = false } = opts;
  const [data, setData] = useState<T | undefined>(undefined);
  const [status, setStatus] = useState<FeedStatus>("idle");
  const [error, setError] = useState<string | undefined>(undefined);
  const abortRef = useRef<AbortController | null>(null);
  // Manual-refresh trigger — bumping this reruns the effect.
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (disabled || !url) {
      setStatus("idle");
      return;
    }
    // Cancel any in-flight fetch from a previous effect.
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    setStatus((prev) => (prev === "ok" ? "refreshing" : "loading"));
    let cancelled = false;

    fetch(url, {
      headers: fetchHeaders({ Accept: "application/json" }),
      signal: controller.signal,
      credentials: "same-origin",
    })
      .then(async (res) => {
        if (cancelled) return;
        if (!res.ok) {
          throw new Error(`${res.status} ${res.statusText}`);
        }
        const json = (await res.json()) as T;
        if (cancelled) return;
        setData(json);
        setStatus("ok");
        setError(undefined);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof DOMException && err.name === "AbortError") return;
        setStatus("error");
        setError(err instanceof Error ? err.message : String(err));
      });

    let timer: number | undefined;
    if (intervalMs > 0) {
      timer = window.setTimeout(() => setTick((n) => n + 1), intervalMs);
    }
    return () => {
      cancelled = true;
      controller.abort();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [url, intervalMs, disabled, tick]);

  return {
    data,
    status,
    error,
    refresh: () => setTick((n) => n + 1),
  };
}
