import { useCallback, useMemo, useState } from "preact/hooks";
import { ViewHead } from "../chrome";
import { Button, Chip, Pill, Table } from "../primitives";
import type { PillTone } from "../primitives";
import { useFeed } from "../hooks/useFeed";
import type {
  LessonCandidate,
  LessonCandidatesResponse,
  LessonStatus,
} from "../api/types";
import { fetchHeaders } from "../api/key";

type StatusFilter = LessonStatus | "all";

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

export function LearningView() {
  const [filter, setFilter] = useState<StatusFilter>("all");
  const feed = useFeed<LessonCandidatesResponse>(
    "/api/learning/candidates?limit=200",
  );

  const counts = useMemo(() => {
    const base: Record<StatusFilter, number> = {
      all: 0,
      proposed: 0,
      draft_ready: 0,
      approved: 0,
      applied: 0,
      snoozed: 0,
      rejected: 0,
      reverted: 0,
      stale: 0,
    };
    if (feed.data) {
      base.all = feed.data.candidates.length;
      for (const c of feed.data.candidates) {
        if (c.status in base) base[c.status] += 1;
      }
    }
    return base;
  }, [feed.data]);

  const filtered = useMemo(() => {
    if (!feed.data) return [];
    if (filter === "all") return feed.data.candidates;
    return feed.data.candidates.filter((c) => c.status === filter);
  }, [feed.data, filter]);

  const awaitingTriage = counts.proposed + counts.draft_ready;

  const doTransition = useCallback(
    async (lessonId: string, action: "approve" | "reject" | "snooze") => {
      const res = await fetch(
        `/api/learning/candidates/${encodeURIComponent(lessonId)}/${action}`,
        {
          method: "POST",
          headers: fetchHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({ reason: `Operator UI ${action}` }),
        },
      );
      if (!res.ok) {
        const text = await res.text();
        alert(`Transition failed (${res.status}): ${text.slice(0, 300)}`);
        return;
      }
      feed.refresh();
    },
    [feed],
  );

  return (
    <>
      <ViewHead
        sup="Ops · learning"
        title="Lessons"
        sub="Proposed → applied triage queue. States reuse the existing /api/learning transitions."
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
            onClick={() => setFilter(f.value)}
          />
        ))}
      </div>

      {feed.status === "loading" && !feed.data && (
        <div class="op-loading">Loading lessons…</div>
      )}
      {feed.status === "error" && (
        <div class="op-error">Failed to load lessons: {feed.error}</div>
      )}
      {feed.data && (
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
              render: (c) => <span class="mono">{c.lesson_id}</span>,
            },
            {
              key: "pattern",
              label: "Pattern",
              render: (c) => (
                <span>
                  <span class="mono" style={{ color: "var(--ink-600)" }}>
                    {c.detector_name}
                  </span>
                  <br />
                  <span style={{ color: "var(--ink-800)" }}>
                    {c.pattern_key}
                  </span>
                </span>
              ),
            },
            {
              key: "profile",
              label: "Profile",
              width: "140px",
              render: (c) => (
                <span class="mono">{c.client_profile || "—"}</span>
              ),
            },
            {
              key: "freq",
              label: "Freq",
              numeric: true,
              width: "70px",
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
              render: (c) => (
                <LessonActions
                  candidate={c}
                  onAction={(action) => doTransition(c.lesson_id, action)}
                />
              ),
            },
          ]}
        />
      )}
    </>
  );
}

function LessonActions({
  candidate,
  onAction,
}: {
  candidate: LessonCandidate;
  onAction: (a: "approve" | "reject" | "snooze") => void;
}) {
  const { status } = candidate;
  if (status === "proposed" || status === "draft_ready") {
    return (
      <div style={{ display: "flex", gap: "4px" }}>
        <Button size="sm" variant="primary" onClick={() => onAction("approve")}>
          Approve
        </Button>
        <Button size="sm" onClick={() => onAction("snooze")}>
          Snooze
        </Button>
        <Button size="sm" variant="danger" onClick={() => onAction("reject")}>
          Reject
        </Button>
      </div>
    );
  }
  if (status === "snoozed") {
    return (
      <Button size="sm" onClick={() => onAction("approve")}>
        Re-open
      </Button>
    );
  }
  return <span style={{ color: "var(--ink-500)" }}>—</span>;
}
