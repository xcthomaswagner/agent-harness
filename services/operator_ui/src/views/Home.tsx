import { ViewHead } from "../chrome";
import { Pill, SectionHeader, Table } from "../primitives";
import { useFeed } from "../hooks/useFeed";
import type {
  LessonCountsResponse,
  ProfileSummary,
  ProfilesResponse,
  TraceSummary,
  TracesResponse,
} from "../api/types";
import { href, navigate } from "../router";
import { intOrDash, pct } from "./format";
import {
  filterRuns,
  runBucketCounts,
  runOutcomeLabel,
  runOutcomeTone,
} from "./runModel";
import "./views.css";

/**
 * Home view: command-center summary for operator decisions.
 *
 * Data sources:
 *   - GET /api/operator/profiles — client readiness/performance summaries
 *   - GET /api/operator/lessons/counts — 8-state lesson count strip
 *   - GET /api/operator/traces — run list used for attention buckets
 */
export function HomeView() {
  const profiles = useFeed<ProfilesResponse>("/api/operator/profiles");
  const lessons = useFeed<LessonCountsResponse>("/api/operator/lessons/counts");
  const traces = useFeed<TracesResponse>("/api/operator/traces?limit=500");

  const rows = traces.data?.traces ?? [];
  const runCounts = runBucketCounts(rows);
  const attentionRows = filterRuns(rows, "attention").slice(0, 5);
  const activeRows = filterRuns(rows, "active").slice(0, 5);
  const successfulRows = filterRuns(rows, "successful").slice(0, 5);

  return (
    <>
      <ViewHead
        sup="Operate · command center"
        title="Command Center"
        sub="Current work, intervention points, and improvement signals."
        rnum={traces.status === "loading" && !traces.data ? "—" : String(runCounts.attention)}
        rlabel="Need attention"
      />

      <section class="op-section">
        <SectionHeader
          label="Needs attention"
          right={<a href={href({ name: "runs" })}>All runs →</a>}
        />
        <RunFocusList
          state={traces.status}
          rows={attentionRows}
          empty="No runs need attention."
        />
      </section>

      <section class="op-command-grid">
        <div>
          <SectionHeader
            label="Active runs"
            right={`${runCounts.active} active`}
          />
          <RunFocusList
            state={traces.status}
            rows={activeRows}
            empty="No active runs."
            compact
          />
        </div>

        <div>
          <SectionHeader
            label="Lessons"
            right={<a href={href({ name: "learning" })}>Review lessons →</a>}
          />
          <LessonsStrip counts={lessons.data?.counts} state={lessons.status} />
        </div>
      </section>

      <section class="op-section">
        <SectionHeader
          label="Client health"
          right={
            profiles.data
              ? `${profiles.data.profiles.length} total`
              : profiles.status.toUpperCase()
          }
        />
        <ProfilesGrid state={profiles.status} profiles={profiles.data?.profiles} />
      </section>

      <section class="op-section">
        <SectionHeader
          label="Recent successful runs"
          right={<a href={href({ name: "runs" })}>All runs →</a>}
        />
        <RunFocusList
          state={traces.status}
          rows={successfulRows}
          empty="No successful runs in this window."
        />
      </section>
    </>
  );
}

function ProfilesGrid({
  state,
  profiles,
}: {
  state: string;
  profiles: readonly ProfileSummary[] | undefined;
}) {
  if (state === "loading") return <div class="op-loading">Loading profiles…</div>;
  if (state === "error") {
    return <div class="op-error">Failed to load profiles</div>;
  }
  if (!profiles || profiles.length === 0) {
    return <div class="op-empty">No client profiles configured yet.</div>;
  }
  return (
    <div class="op-profiles-grid">
      {profiles.map((p) => (
        <ProfileCard key={p.id} profile={p} />
      ))}
    </div>
  );
}

function ProfileCard({ profile: p }: { profile: ProfileSummary }) {
  return (
    <a
      class="op-profile-card"
      href={href({ name: "autonomy", profile: p.id })}
    >
      <div class="op-profile-card-head">
        <span class="op-profile-name">{p.name}</span>
        <span class="op-profile-sample">{p.sample || "—"}</span>
      </div>

      <div class="op-profile-metrics">
        <ProfileMetric label="First-pass" value={p.fpa} />
        <ProfileMetric label="Escapes" value={p.escape} />
        <ProfileMetric label="Auto-merge" value={p.auto_merge} />
      </div>

      <div class="op-profile-footer">
        {p.in_flight > 0 ? (
          <Pill tone="active">
            {p.in_flight} in-flight
          </Pill>
        ) : (
          <Pill tone="cool">idle</Pill>
        )}
        <Pill tone={p.completed_24h > 0 ? "ok" : "cool"}>
          {intOrDash(p.completed_24h)} · 24h done
        </Pill>
      </div>
    </a>
  );
}

function ProfileMetric({
  label,
  value,
}: {
  label: string;
  value: number | null;
}) {
  const missing = value === null;
  return (
    <div class="op-profile-metric">
      <span class="op-profile-metric-label">{label}</span>
      <span class={`op-profile-metric-val${missing ? " is-missing" : ""}`}>
        {pct(value)}
      </span>
    </div>
  );
}

function RunFocusList({
  state,
  rows,
  empty,
  compact = false,
}: {
  state: string;
  rows: readonly TraceSummary[] | undefined;
  empty: string;
  compact?: boolean;
}) {
  if (state === "loading") return <div class="op-loading">Loading runs…</div>;
  if (state === "error") return <div class="op-error">Failed to load runs</div>;
  if (!rows || rows.length === 0) {
    return <div class="op-empty">{empty}</div>;
  }
  return (
    <Table<TraceSummary>
      rowKey={(t) => t.id}
      rows={rows}
      isLive={(t) => t.status === "in-flight" || t.status === "queued"}
      onRowClick={(t) => navigate(`/traces/${encodeURIComponent(t.id)}`)}
      large={!compact}
      columns={[
        {
          key: "id",
          label: "Ticket",
          width: "140px",
          render: (t) => <span class="op-mono">{t.id}</span>,
        },
        { key: "title", label: "Title", render: (t) => t.title || "—" },
        {
          key: "status",
          label: "Outcome",
          width: "150px",
          render: (t) => (
            <span title={t.raw_status}>
              <Pill tone={runOutcomeTone(t)}>{runOutcomeLabel(t)}</Pill>
            </span>
          ),
        },
        {
          key: "phase",
          label: "Phase",
          width: "130px",
          render: (t) => <span class="op-mono">{t.phase || "—"}</span>,
        },
        {
          key: "elapsed",
          label: "Elapsed",
          width: "90px",
          numeric: true,
          render: (t) => t.elapsed || "—",
        },
      ]}
    />
  );
}

function LessonsStrip({
  counts,
  state,
}: {
  counts: LessonCountsResponse["counts"] | undefined;
  state: string;
}) {
  if (state === "loading" || !counts) {
    return <div class="op-loading">Loading lessons…</div>;
  }
  if (state === "error") {
    return <div class="op-error">Failed to load lesson counts</div>;
  }

  // Design shows 6 cells; we collapse the 8 backend states so rare
  // lifecycle states (reverted, stale) don't eat display slots.
  const cells: {
    label: string;
    value: number;
    target?: "proposed" | "approved" | "applied" | "snoozed" | "rejected";
  }[] = [
    { label: "Proposed", value: counts.proposed, target: "proposed" },
    { label: "Draft", value: counts.draft_ready },
    { label: "Approved", value: counts.approved, target: "approved" },
    { label: "Applied", value: counts.applied, target: "applied" },
    { label: "Snoozed", value: counts.snoozed, target: "snoozed" },
    { label: "Rejected", value: counts.rejected, target: "rejected" },
  ];

  return (
    <div class="op-lessons-strip">
      {cells.map((c) => (
        <a
          key={c.label}
          class="op-lessons-cell"
          href={href({ name: "learning" })}
        >
          <span class="op-lessons-cell-n">{c.value}</span>
          <span class="op-lessons-cell-lbl">{c.label}</span>
        </a>
      ))}
    </div>
  );
}
