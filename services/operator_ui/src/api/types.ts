/**
 * TypeScript types mirroring the /api/operator JSON contracts.
 *
 * Keep 1:1 with the shapes returned by services/l1_preprocessing/
 * operator_api_data.py. When the backend changes shape, bump the route
 * path (``/v2/...``) rather than silently mutating this file.
 */

export interface ProfileSummary {
  id: string;
  name: string;
  sample: string;
  in_flight: number;
  completed_24h: number;
  /** Proportion 0..1 or null when sample size too small. */
  fpa: number | null;
  escape: number | null;
  catch: number | null;
  /** Proportion 0..1. Zero when no decisions in the window. */
  auto_merge: number;
}

export interface ProfilesResponse {
  profiles: ProfileSummary[];
}

export type LessonState =
  | "proposed"
  | "draft_ready"
  | "approved"
  | "applied"
  | "snoozed"
  | "rejected"
  | "reverted"
  | "stale";

export type LessonCounts = Record<LessonState, number>;

export interface LessonCountsResponse {
  counts: LessonCounts;
}

export type TraceStatus = "in-flight" | "stuck" | "queued" | "done";

export interface TraceSummary {
  id: string;
  title: string;
  status: TraceStatus;
  raw_status: string;
  phase: string;
  elapsed: string;
  started_at: string;
  pr_url: string | null;
  pipeline_mode: string;
  review_verdict: string;
  qa_result: string;
}

export interface TracesResponse {
  traces: TraceSummary[];
  count: number;
  offset: number;
  limit: number;
}
