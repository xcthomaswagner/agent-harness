package dashboard

import "testing"

func TestRunBucketCountsMatchOperatorModel(t *testing.T) {
	rows := []TraceSummary{
		{ID: "A", Status: "in-flight", RawStatus: "Running"},
		{ID: "B", Status: "stuck", RawStatus: "Blocked"},
		{ID: "C", Status: "done", RawStatus: "Complete"},
		{ID: "D", Status: "done", RawStatus: "Failed"},
		{ID: "E", Status: "done", RawStatus: "Submitted", Elapsed: ">24h"},
		{ID: "F", Status: "hidden", RawStatus: "Suppressed", Hidden: true},
	}

	counts := RunBucketCounts(rows)

	if counts[BucketActive] != 1 {
		t.Fatalf("active = %d, want 1", counts[BucketActive])
	}
	if counts[BucketAttention] != 3 {
		t.Fatalf("attention = %d, want 3", counts[BucketAttention])
	}
	if counts[BucketSuccessful] != 1 {
		t.Fatalf("successful = %d, want 1", counts[BucketSuccessful])
	}
	if counts[BucketFailed] != 1 {
		t.Fatalf("failed = %d, want 1", counts[BucketFailed])
	}
	if counts[BucketHidden] != 1 {
		t.Fatalf("hidden = %d, want 1", counts[BucketHidden])
	}
}

func TestRunOutcomePrioritizesFailureBeforeAttention(t *testing.T) {
	row := TraceSummary{ID: "FAIL-1", Status: "done", RawStatus: "Escalated", LifecycleState: "stale"}

	if got := RunOutcomeLabel(row); got != "Failed" {
		t.Fatalf("RunOutcomeLabel() = %q, want Failed", got)
	}
}

func TestFilterRunsExcludesHiddenFromRecent(t *testing.T) {
	rows := []TraceSummary{
		{ID: "A", Status: "done", RawStatus: "Complete"},
		{ID: "B", Status: "hidden", RawStatus: "Suppressed", Hidden: true},
	}

	recent := FilterRuns(rows, BucketRecent)
	if len(recent) != 1 || recent[0].ID != "A" {
		t.Fatalf("recent = %#v, want only A", recent)
	}
}
