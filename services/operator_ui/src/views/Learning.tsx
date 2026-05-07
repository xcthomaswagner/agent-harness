import { useCallback, useMemo, useState } from "preact/hooks";
import { ViewHead } from "../chrome";
import { Button, Chip, Pill, Table } from "../primitives";
import type { PillTone } from "../primitives";
import { useFeed } from "../hooks/useFeed";
import type {
  LessonCandidate,
  LessonCandidatesResponse,
  LessonCountsResponse,
  LessonStatus,
} from "../api/types";
import { fetchHeaders } from "../api/key";
import { parseJsonObject, readableError } from "./actionFeedback";
import type { ActionNotice } from "./actionFeedback";

// Filter chips cover the 6 operator-relevant states the design shows.
// ``reverted`` and ``stale`` are lifecycle-only — they get a pill tone
// below so they render correctly when they appear, but aren't first-class
// filter targets. If an operator needs to find them, the "all" filter
// still surfaces them.
type FilterableStatus = Exclude<LessonStatus, "reverted" | "stale">;
type StatusFilter = FilterableStatus | "all";
type LearningAction = "draft" | "approve" | "reject" | "snooze";

const SNOOZE_DAYS = 7;

const FILTERS: readonly { label: string; value: StatusFilter }[] = [
  { label: "All", value: "all" },
  { label: "Proposed", value: "proposed" },
  { label: "Draft", value: "draft_ready" },
  { label: "Approved", value: "approved" },
  { label: "Applied", value: "applied" },
  { label: "Snoozed", value: "snoozed" },
  { label: "Rejected", value: "rejected" },
];

const STATUS_TONE: Record<LessonStatus, PillTone> = {
  proposed: "cool",
  draft_ready: "warn",
  approved: "active",
  applied: "ok",
  snoozed: "cool",
  rejected: "err",
  reverted: "err",
  stale: "cool",
};

type LessonImpact = "critical" | "high" | "medium" | "low";

interface LessonImpactInfo {
  level: LessonImpact;
  reason: string;
  tone: PillTone;
}

const IMPACT_RANK: Record<LessonImpact, number> = {
  critical: 0,
  high: 1,
  medium: 2,
  low: 3,
};
const PAGE_SIZE = 200;

export function LearningView() {
  const [filter, setFilter] = useState<StatusFilter>("all");
  const [offset, setOffset] = useState(0);
  const [pendingLessonId, setPendingLessonId] = useState<string | null>(null);
  const [notice, setNotice] = useState<{
    tone: ActionNotice["tone"];
    text: string;
  } | null>(null);
  const statusQuery = filter === "all" ? "" : `&status=${encodeURIComponent(filter)}`;
  const feed = useFeed<LessonCandidatesResponse>(
    `/api/learning/candidates?limit=${PAGE_SIZE}&offset=${offset}${statusQuery}`,
    { clearOnUrlChange: true },
  );
  const lessonCounts = useFeed<LessonCountsResponse>("/api/operator/lessons/counts");

  const counts = useMemo(() => {
    const base: Record<StatusFilter, number> = {
      all: 0,
      proposed: 0,
      draft_ready: 0,
      approved: 0,
      applied: 0,
      snoozed: 0,
      rejected: 0,
    };
    if (lessonCounts.data) {
      const raw = lessonCounts.data.counts;
      base.proposed = raw.proposed;
      base.draft_ready = raw.draft_ready;
      base.approved = raw.approved;
      base.applied = raw.applied;
      base.snoozed = raw.snoozed;
      base.rejected = raw.rejected;
      base.all = Object.values(raw).reduce((sum, value) => sum + value, 0);
    } else if (feed.data) {
      base.all = feed.data.total ?? feed.data.count ?? feed.data.candidates.length;
    }
    return base;
  }, [feed.data, lessonCounts.data]);

  const filtered = useMemo(() => {
    if (!feed.data) return [];
    const rows = feed.data.candidates;
    return [...rows].sort((a, b) => {
      const ai = lessonImpact(a);
      const bi = lessonImpact(b);
      return (
        IMPACT_RANK[ai.level] - IMPACT_RANK[bi.level] ||
        b.frequency - a.frequency ||
        a.pattern_key.localeCompare(b.pattern_key)
      );
    });
  }, [feed.data]);

  const awaitingTriage = counts.proposed + counts.draft_ready;
  const pageCount = feed.data?.count ?? feed.data?.candidates.length ?? 0;
  const pageTotal = feed.data?.total ?? counts[filter] ?? pageCount;
  const pageOffset = feed.data?.offset ?? offset;

  const doTransition = useCallback(
    async (lessonId: string, action: LearningAction) => {
      setPendingLessonId(lessonId);
      setNotice({
        tone: "warn",
        text: `${actionLabel(action)} in progress for ${lessonId}...`,
      });
      try {
        const payload = transitionPayload(action);
        const res = await fetch(
          `/api/learning/candidates/${encodeURIComponent(lessonId)}/${action}`,
          {
            method: "POST",
            headers: fetchHeaders({ "Content-Type": "application/json" }),
            body: JSON.stringify(payload),
          },
        );
        const text = await res.text();
        const body = parseJsonObject(text);
        if (!res.ok) {
          const detail = readableError(body, text);
          setNotice({
            tone: "err",
            text: `${actionLabel(action)} failed (${res.status}): ${detail}`,
          });
          return;
        }
        if (action === "draft" && body && body["status"] === "proposed") {
          const detail = readableError(body, "Draft failed; lesson remains proposed.");
          setNotice({
            tone: "warn",
            text: `Draft did not advance ${lessonId}: ${detail}`,
          });
        } else {
          const nextReviewAt =
            action === "snooze" ? payload.next_review_at : undefined;
          const suffix = nextReviewAt
            ? ` until ${formatReviewDate(nextReviewAt)}`
            : "";
          setNotice({
            tone: "ok",
            text: `${actionLabel(action)} complete for ${lessonId}${suffix}.`,
          });
        }
        feed.refresh();
        lessonCounts.refresh();
      } catch (err) {
        const detail = err instanceof Error ? err.message : String(err);
        setNotice({
          tone: "err",
          text: `${actionLabel(action)} failed: ${detail}`,
        });
      } finally {
        setPendingLessonId(null);
      }
    },
    [feed, lessonCounts],
  );

  return (
    <>
      <ViewHead
        sup="Improve · learning"
        title="Lessons"
        sub="Reusable harness improvements awaiting operator triage."
        rnum={String(awaitingTriage)}
        rlabel="Awaiting triage"
      />

      <div style={{ display: "flex", gap: "8px", marginBottom: "24px", flexWrap: "wrap" }}>
        {FILTERS.map((f) => (
          <Chip
            key={f.value}
            label={f.label}
            count={counts[f.value]}
            on={filter === f.value}
            onClick={() => {
              setFilter(f.value);
              setOffset(0);
            }}
          />
        ))}
      </div>

      {notice && (
        <div class={`op-action-notice is-${notice.tone}`} role="status">
          {notice.text}
        </div>
      )}
      {feed.status === "loading" && !feed.data && (
        <div class="op-loading">Loading lessons…</div>
      )}
      {feed.status === "error" && (
        <div class="op-error">Failed to load lessons: {feed.error}</div>
      )}
      {feed.data && (
        <>
          <div class="op-table-tools">
            <span class="op-muted">
              Showing {pageCount} of {pageTotal} · offset {pageOffset}
            </span>
            <div style={{ display: "flex", gap: "8px" }}>
              <Button
                size="sm"
                disabled={offset === 0}
                onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
              >
                Previous
              </Button>
              <Button
                size="sm"
                disabled={offset + PAGE_SIZE >= pageTotal}
                onClick={() => setOffset(offset + PAGE_SIZE)}
              >
                Next
              </Button>
            </div>
          </div>
          <Table<LessonCandidate>
            large
            rowKey={(c) => c.lesson_id}
            rows={filtered}
            empty={
              filter === "all"
                ? "No lessons proposed yet."
                : `No ${filter} lessons.`
            }
            columns={[
            {
              key: "id",
              label: "Lesson",
              width: "120px",
              sortValue: (c) => c.lesson_id,
              render: (c) => <span class="op-mono">{c.lesson_id}</span>,
            },
            {
              key: "pattern",
              label: "Pattern",
              sortValue: (c) => `${c.detector_name} ${c.pattern_key}`,
              render: (c) => (
                <span>
                  <span class="op-mono" style={{ color: "var(--ink-600)" }}>
                    {c.detector_name}
                  </span>
                  <br />
                  <span style={{ color: "var(--ink-800)" }}>
                    {c.pattern_key}
                  </span>
                  {c.status_reason && (
                    <>
                      <br />
                      <span class="op-row-reason">{c.status_reason}</span>
                    </>
                  )}
                </span>
              ),
            },
            {
              key: "profile",
              label: "Profile",
              width: "140px",
              sortValue: (c) => c.client_profile || "",
              render: (c) => (
                <span class="op-mono">{c.client_profile || "—"}</span>
              ),
            },
            {
              key: "impact",
              label: "Impact",
              width: "130px",
              sortValue: (c) => IMPACT_RANK[lessonImpact(c).level],
              render: (c) => {
                const impact = lessonImpact(c);
                return (
                  <span class="op-impact-cell">
                    <Pill tone={impact.tone}>{impact.level}</Pill>
                    <span class="op-impact-reason">{impact.reason}</span>
                  </span>
                );
              },
            },
            {
              key: "freq",
              label: "Freq",
              numeric: true,
              width: "70px",
              sortValue: (c) => c.frequency,
              render: (c) => c.frequency,
            },
            {
              key: "status",
              label: "State",
              width: "120px",
              render: (c) => (
                <Pill tone={STATUS_TONE[c.status]}>{c.status}</Pill>
              ),
            },
            {
              key: "actions",
              label: "",
              width: "220px",
              sortValue: (c) => c.status,
              render: (c) => (
                <LessonActions
                  candidate={c}
                  disabled={pendingLessonId !== null}
                  onAction={(action) => doTransition(c.lesson_id, action)}
                />
              ),
            },
            ]}
          />
        </>
      )}
    </>
  );
}

function LessonActions({
  candidate,
  disabled,
  onAction,
}: {
  candidate: LessonCandidate;
  disabled: boolean;
  onAction: (a: LearningAction) => void;
}) {
  const { status } = candidate;
  if (status === "proposed") {
    return (
      <div style={{ display: "flex", gap: "4px" }}>
        <Button size="sm" variant="primary" disabled={disabled} onClick={() => onAction("draft")}>
          Draft
        </Button>
        <Button size="sm" disabled={disabled} onClick={() => onAction("snooze")}>
          Snooze
        </Button>
        <Button size="sm" variant="danger" disabled={disabled} onClick={() => onAction("reject")}>
          Reject
        </Button>
      </div>
    );
  }
  if (status === "draft_ready" || status === "approved") {
    return (
      <div style={{ display: "flex", gap: "4px" }}>
        <Button size="sm" variant="primary" disabled={disabled} onClick={() => onAction("approve")}>
          Approve
        </Button>
        <Button size="sm" variant="danger" disabled={disabled} onClick={() => onAction("reject")}>
          Reject
        </Button>
      </div>
    );
  }
  if (status === "snoozed") {
    return (
      <div style={{ display: "flex", gap: "4px" }}>
        <Button size="sm" variant="danger" disabled={disabled} onClick={() => onAction("reject")}>
          Reject
        </Button>
      </div>
    );
  }
  return <span style={{ color: "var(--ink-500)" }}>—</span>;
}

function actionLabel(action: LearningAction): string {
  return action.slice(0, 1).toUpperCase() + action.slice(1);
}

function transitionPayload(action: LearningAction): {
  reason: string;
  next_review_at?: string;
} {
  if (action !== "snooze") return { reason: `Operator UI ${action}` };
  const nextReviewAt = new Date(
    Date.now() + SNOOZE_DAYS * 24 * 60 * 60 * 1000,
  ).toISOString();
  return {
    reason: `Operator UI snooze ${SNOOZE_DAYS}d`,
    next_review_at: nextReviewAt,
  };
}

function formatReviewDate(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleDateString([], {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

function lessonImpact(candidate: LessonCandidate): LessonImpactInfo {
  const haystack = [
    candidate.severity,
    candidate.detector_name,
    candidate.pattern_key,
    candidate.proposed_delta_json,
  ]
    .join(" ")
    .toLowerCase();

  if (
    /\b(credential|secret|token|authorization|auth header|leak|exposure)\b/.test(
      haystack,
    )
  ) {
    return { level: "critical", reason: "Credential risk", tone: "err" };
  }

  if (
    /\b(bypass\w*|wrong branch|production|false pass|schema writes?|cma writes?)\b/.test(
      haystack,
    )
  ) {
    return { level: "critical", reason: "Delivery control", tone: "err" };
  }

  if (
    /\b(xss|javascript:|protocol allowlist|semgrep|gitleaks|security)\b/.test(
      haystack,
    )
  ) {
    return { level: "high", reason: "Security", tone: "warn" };
  }

  if (
    /\b(oauth|pre-flight|preflight|round-trip|verification|blocked|failed delivery)\b/.test(
      haystack,
    )
  ) {
    return { level: "high", reason: "Run blocker", tone: "warn" };
  }

  if (/\b(a11y|accessibility|heading|aria|alt text)\b/.test(haystack)) {
    return { level: "medium", reason: "Quality", tone: "active" };
  }

  if (/\b(screenshot|e2e|visual|qa|test coverage)\b/.test(haystack)) {
    return { level: "medium", reason: "Validation", tone: "active" };
  }

  const severity = candidate.severity.toLowerCase();
  if (severity === "critical") {
    return { level: "critical", reason: "Critical", tone: "err" };
  }
  if (severity === "warn" || severity === "warning") {
    return { level: "high", reason: "Likely rework", tone: "warn" };
  }
  if (severity === "info") {
    return { level: "medium", reason: "Improvement", tone: "active" };
  }
  return { level: "low", reason: "Monitor", tone: "cool" };
}
