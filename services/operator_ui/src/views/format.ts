/** Small formatting helpers shared across views. */

import type { TraceStatus, TracesResponse } from "../api/types";

type StatusFilter = TraceStatus | "all";

/** Render a proportion (0..1 or null) as "NN%" or "—". */
export function pct(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return `${Math.round(value * 100)}%`;
}

/** Render an integer count, falling back to "—" for null/undefined. */
export function intOrDash(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return String(value);
}

export function emptyTraceCounts(): Record<StatusFilter, number> {
  return {
    all: 0,
    "in-flight": 0,
    stuck: 0,
    queued: 0,
    done: 0,
    hidden: 0,
  };
}

export function traceCountsFromResponse(
  data: TracesResponse | undefined,
): Record<StatusFilter, number> {
  const counts = emptyTraceCounts();
  if (!data) return counts;
  if (data.status_counts) {
    Object.assign(counts, data.status_counts);
    return counts;
  }
  counts.all = data.count ?? data.traces.length;
  for (const trace of data.traces) {
    counts[trace.status] += 1;
  }
  return counts;
}
