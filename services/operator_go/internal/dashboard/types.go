package dashboard

type TraceSummary struct {
	ID             string `json:"id"`
	Title          string `json:"title"`
	Status         string `json:"status"`
	RawStatus      string `json:"raw_status"`
	Hidden         bool   `json:"hidden"`
	LifecycleState string `json:"lifecycle_state"`
	StateReason    string `json:"state_reason"`
	RunID          string `json:"run_id"`
	Phase          string `json:"phase"`
	Elapsed        string `json:"elapsed"`
	StartedAt      string `json:"started_at"`
	PRURL          string `json:"pr_url"`
	PipelineMode   string `json:"pipeline_mode"`
	ReviewVerdict  string `json:"review_verdict"`
	QAResult       string `json:"qa_result"`
}

type RunBucket string

const (
	BucketActive     RunBucket = "active"
	BucketAttention  RunBucket = "attention"
	BucketRecent     RunBucket = "recent"
	BucketSuccessful RunBucket = "successful"
	BucketFailed     RunBucket = "failed"
	BucketHidden     RunBucket = "hidden"
)

type BucketCounts map[RunBucket]int
