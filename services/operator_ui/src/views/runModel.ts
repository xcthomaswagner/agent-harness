import type { TraceSummary } from "../api/types";
import type { PillTone } from "../primitives";

export type RunBucket =
  | "active"
  | "attention"
  | "recent"
  | "successful"
  | "failed"
  | "hidden";

export const RUN_BUCKETS: readonly { label: string; value: RunBucket }[] = [
  { label: "Active", value: "active" },
  { label: "Needs attention", value: "attention" },
  { label: "Recent", value: "recent" },
  { label: "Successful", value: "successful" },
  { label: "Failed", value: "failed" },
  { label: "Hidden", value: "hidden" },
];

export function runBucketCounts(rows: readonly TraceSummary[] | undefined): Record<RunBucket, number> {
  const counts: Record<RunBucket, number> = {
    active: 0,
    attention: 0,
    recent: 0,
    successful: 0,
    failed: 0,
    hidden: 0,
  };
  for (const row of rows ?? []) {
    if (row.hidden || row.status === "hidden") {
      counts.hidden += 1;
      continue;
    }
    counts.recent += 1;
    if (isActiveRun(row)) counts.active += 1;
    if (needsAttention(row)) counts.attention += 1;
    if (isSuccessfulRun(row)) counts.successful += 1;
    if (isFailedRun(row)) counts.failed += 1;
  }
  return counts;
}

export function filterRuns(
  rows: readonly TraceSummary[] | undefined,
  bucket: RunBucket,
): TraceSummary[] {
  const source = [...(rows ?? [])];
  switch (bucket) {
    case "active":
      return source.filter((row) => !isHiddenRun(row) && isActiveRun(row));
    case "attention":
      return source.filter((row) => !isHiddenRun(row) && needsAttention(row));
    case "successful":
      return source.filter((row) => !isHiddenRun(row) && isSuccessfulRun(row));
    case "failed":
      return source.filter((row) => !isHiddenRun(row) && isFailedRun(row));
    case "hidden":
      return source.filter(isHiddenRun);
    case "recent":
    default:
      return source.filter((row) => !isHiddenRun(row));
  }
}

export function runOutcomeLabel(row: TraceSummary): string {
  if (isHiddenRun(row)) return "Hidden";
  if (isActiveRun(row)) return row.status === "queued" ? "Queued" : "Active";
  if (isFailedRun(row)) return "Failed";
  if (needsAttention(row)) return "Needs attention";
  if (isSuccessfulRun(row)) return "Successful";
  return titleCase(row.raw_status || row.status || "Run");
}

export function runOutcomeTone(row: TraceSummary): PillTone {
  if (isHiddenRun(row)) return "err";
  if (isActiveRun(row)) return "active";
  if (isFailedRun(row)) return "err";
  if (needsAttention(row)) return "warn";
  if (isSuccessfulRun(row)) return "ok";
  return "cool";
}

export function isLiveRun(row: TraceSummary | null | undefined): boolean {
  return Boolean(row && !isHiddenRun(row) && isActiveRun(row));
}

export function isHiddenRun(row: TraceSummary): boolean {
  return row.hidden || row.status === "hidden";
}

export function isActiveRun(row: TraceSummary): boolean {
  return row.status === "in-flight" || row.status === "queued";
}

export function needsAttention(row: TraceSummary): boolean {
  if (row.status === "stuck") return true;
  if (row.lifecycle_state === "stale") return true;
  if (isFailedRun(row)) return true;
  const raw = normalizedRaw(row);
  if (raw === "submitted" && row.elapsed.includes(">24h")) return true;
  return false;
}

export function isSuccessfulRun(row: TraceSummary): boolean {
  if (isFailedRun(row)) return false;
  const raw = normalizedRaw(row);
  return (
    row.status === "done" &&
    (raw === "complete" ||
      raw === "completed" ||
      raw === "pr created" ||
      raw === "merged" ||
      raw === "closed")
  );
}

export function isFailedRun(row: TraceSummary): boolean {
  const raw = normalizedRaw(row);
  return (
    raw.includes("fail") ||
    raw.includes("skip") ||
    raw.includes("escalat") ||
    raw.includes("error")
  );
}

function normalizedRaw(row: TraceSummary): string {
  return (row.raw_status || row.status || "").trim().toLowerCase();
}

function titleCase(value: string): string {
  return value
    .split(/[\s_-]+/)
    .filter(Boolean)
    .map((part) => part.slice(0, 1).toUpperCase() + part.slice(1).toLowerCase())
    .join(" ");
}
