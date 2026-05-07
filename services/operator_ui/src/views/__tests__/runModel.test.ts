import { describe, expect, it } from "vitest";
import type { TraceSummary } from "../../api/types";
import {
  filterRuns,
  runBucketCounts,
  runOutcomeLabel,
  runOutcomeTone,
} from "../runModel";

describe("operator run model", () => {
  it("separates active, attention, successful, failed, and hidden runs", () => {
    const rows = [
      trace("ACTIVE", "in-flight", "In Flight"),
      trace("STUCK", "stuck", "Stuck"),
      trace("DONE", "done", "Complete"),
      trace("FAIL", "done", "Failed"),
      { ...trace("HIDE", "hidden", "Complete"), hidden: true },
    ];

    expect(runBucketCounts(rows)).toEqual({
      active: 1,
      attention: 2,
      recent: 4,
      successful: 1,
      failed: 1,
      hidden: 1,
    });
    expect(filterRuns(rows, "attention").map((row) => row.id)).toEqual([
      "STUCK",
      "FAIL",
    ]);
    expect(filterRuns(rows, "successful").map((row) => row.id)).toEqual(["DONE"]);
  });

  it("surfaces submitted multi-day runs as attention", () => {
    const row = { ...trace("OLD", "done", "Submitted"), elapsed: ">24h (multi-run)" };

    expect(filterRuns([row], "attention")).toHaveLength(1);
    expect(runOutcomeLabel(row)).toBe("Needs attention");
    expect(runOutcomeTone(row)).toBe("warn");
  });
});

function trace(id: string, status: TraceSummary["status"], rawStatus: string): TraceSummary {
  return {
    id,
    title: "Test run",
    status,
    raw_status: rawStatus,
    hidden: false,
    lifecycle_state: "",
    state_reason: "",
    run_id: `trace-${id}`,
    phase: "",
    elapsed: "1m",
    started_at: "2026-05-04T12:00:00+00:00",
    pr_url: null,
    pipeline_mode: "",
    review_verdict: "",
    qa_result: "",
  };
}
