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

export interface ModelPolicyRole {
  role: string;
  label: string;
  model: string;
  reasoning: string;
}

export interface ModelPolicyResponse {
  version: number;
  source: "default" | "local";
  updated_at?: string;
  model_options: string[];
  reasoning_options: string[];
  roles: ModelPolicyRole[];
}

export interface OperatorSystemResponse {
  service: string;
  version: string;
  pid: number;
  started_at: string;
  uptime_seconds: number;
  git_sha: string;
  git_branch: string;
  code_path: string;
  db_path: string;
  operator_bundle: {
    rev: string;
    built_at: string;
  };
}

export type TraceStatus = "in-flight" | "stuck" | "queued" | "done" | "hidden";

export interface TraceSummary {
  id: string;
  title: string;
  status: TraceStatus;
  raw_status: string;
  hidden: boolean;
  lifecycle_state: string;
  state_reason: string;
  run_id: string;
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
  include_hidden: boolean;
}

export interface TracePhase {
  key: "planning" | "scaffolding" | "implementing" | "reviewing" | "merging";
  name: string;
  state: "done" | "active" | "pending" | "fail";
  duration_seconds: number;
  event_count: number;
}

export interface TraceEvent {
  t: string;
  ev: string;
  phase: string;
  msg: string;
}

export interface TraceDetailResponse {
  id: string;
  title: string;
  status: TraceStatus;
  raw_status: string;
  hidden: boolean;
  lifecycle_state: string;
  state_reason: string;
  run_id: string;
  pipeline_mode: string;
  started_at: string;
  elapsed: string;
  pr_url: string | null;
  review_verdict: string;
  qa_result: string;
  phases: TracePhase[];
  events: TraceEvent[];
}

export interface AutonomyTrendPoint {
  date: string;
  value: number | null;
  sample: number;
}

export interface AutonomyByTypeRow {
  ticket_type: string;
  volume: number;
  fpa: number | null;
  catch: number | null;
  escape: number | null;
  merged: number;
}

export interface AutonomyEscapedDefect {
  id: string;
  ticket_id: string;
  pr_number: number | null;
  severity: string;
  where: string;
  reported_at: string;
  note: string;
}

// Learning candidates — schema matches learning_api._candidate_to_dict.
export type LessonStatus =
  | "proposed"
  | "draft_ready"
  | "approved"
  | "applied"
  | "snoozed"
  | "rejected"
  | "reverted"
  | "stale";

export interface LessonCandidate {
  lesson_id: string;
  detector_name: string;
  pattern_key: string;
  scope_key: string;
  client_profile: string;
  platform_profile: string;
  status: LessonStatus;
  status_reason: string;
  severity: string;
  frequency: number;
  first_seen_at: string;
  last_seen_at: string;
  updated_at: string;
  pr_url: string | null;
  merged_commit_sha: string | null;
  proposed_delta_json: string;
  next_review_at: string | null;
}

export interface LessonCandidatesResponse {
  candidates: LessonCandidate[];
  count: number;
  total: number;
  limit: number;
  offset: number;
}

// PR drilldown — matches operator_api_data.get_pr_detail
export interface PRCommit {
  sha: string;
  message: string;
  author: string;
  authored_at: string;
}

export interface PRIssueMatch {
  ai_issue_id: number;
  confidence: number;
  matched_by: string;
}

export interface PRReviewIssue {
  id: number;
  source: string;
  severity: string;
  category: string;
  summary: string;
  where: string;
  line_start: number | null;
  matched: PRIssueMatch | null;
}

export interface PRLessonMatch {
  lesson_id: string;
  status: string;
  applied: boolean;
  source_ref: string;
  snippet: string;
}

export interface PRAutoMergeDecision {
  decision: string;
  reason: string;
  confidence: number | null;
  created_at: string;
  gates: Record<string, boolean>;
}

export interface AgentRosterEntry {
  teammate: string;
  role: string;
  role_group: "team_lead" | "dev" | "review" | "qa" | "other";
  display_name: string;
  state: "running" | "idle" | "stale";
  last_at: string | null;
  current_activity: string;
  last_tool: string;
  last_event_kind: string;
  tool_uses: number;
  total_tokens: number;
  event_count: number;
  stream_path_present: boolean;
  last_summary: string;
  latest_events: AgentActivityEvent[];
}

export interface AgentActivityEvent {
  event_id: string;
  teammate: string;
  role: string;
  role_group: "team_lead" | "dev" | "review" | "qa" | "other";
  display_name: string;
  kind: string;
  timestamp: string;
  observed_at: string;
  source_line: number | null;
  tool_name?: string;
  text?: string;
  description?: string;
  summary?: string;
  status?: string;
}

export interface AgentRosterResponse {
  agents: AgentRosterEntry[];
}

export interface ActivitySummaryItem {
  event_id: string;
  teammate: string;
  display_name: string;
  role: string;
  role_group: "team_lead" | "dev" | "review" | "qa" | "other";
  kind: string;
  tool_name: string;
  message: string;
  last_at: string;
  count: number;
}

export interface ActivitySummaryTool {
  name: string;
  count: number;
}

export interface ActivitySummaryTeammate {
  teammate: string;
  role: string;
  role_group: "team_lead" | "dev" | "review" | "qa" | "other";
  display_name: string;
  state: string;
  last_at: string | null;
  event_count: number;
  raw_event_count: number;
  deduped_event_count: number;
  tool_uses: number;
  total_tokens: number;
  tools: ActivitySummaryTool[];
  actions: ActivitySummaryItem[];
  warnings: string[];
  current_activity: string;
}

export interface ActivitySummaryResponse {
  ticket_id: string;
  summary: string;
  raw_event_count: number;
  deduped_event_count: number;
  teammates: ActivitySummaryTeammate[];
  highlights: ActivitySummaryItem[];
  warnings: string[];
}

export interface PRDetailResponse {
  pr_run_id: number;
  ticket_id: string;
  pr_number: number;
  repo_full_name: string;
  pr_url: string;
  head_sha: string;
  client_profile: string;
  opened_at: string;
  merged: boolean;
  merged_at: string;
  closed_at: string;
  lifecycle_state: string;
  state_reason: string;
  excluded_from_metrics: boolean;
  first_pass_accepted: boolean;
  commits: PRCommit[];
  issues: PRReviewIssue[];
  matches: PRLessonMatch[];
  auto_merge: PRAutoMergeDecision | null;
  ci_checks_available: boolean;
}

export interface AutonomyResponse {
  profile: string;
  window_days: number;
  metrics: {
    fpa: number | null;
    escape: number | null;
    catch: number | null;
    auto_merge: number;
    sample_size: number;
    merged_count: number;
    recommended_mode: string;
    data_quality_status: string;
  };
  trends: {
    fpa: AutonomyTrendPoint[];
    escape: AutonomyTrendPoint[];
    catch: AutonomyTrendPoint[];
    auto_merge: AutonomyTrendPoint[];
  };
  by_type: AutonomyByTypeRow[];
  escaped: AutonomyEscapedDefect[];
}
