import { ViewHead } from "../chrome";
import { Button, Pill, SectionHeader, Table } from "../primitives";
import type { PillTone } from "../primitives";
import { useFeed } from "../hooks/useFeed";
import type {
  PRDetailResponse,
  PRLessonMatch,
  PRReviewIssue,
} from "../api/types";
import { href } from "../router";

interface Props {
  id: string;
}

const SEVERITY_TONE: Record<string, PillTone> = {
  critical: "err",
  blocker: "err",
  major: "err",
  minor: "warn",
  info: "cool",
};

export function PRDetailView({ id }: Props) {
  // id can be either a numeric pr_run_id or "PR-<num>" — strip the prefix.
  const numericId = id.replace(/^PR-/i, "");
  const feed = useFeed<PRDetailResponse>(
    `/api/operator/pr/${encodeURIComponent(numericId)}`,
  );

  if (feed.status === "loading" && !feed.data) {
    return (
      <>
        <ViewHead sup={`PR · ${id}`} title={id} sub="Loading…" />
        <div class="op-loading">Fetching PR…</div>
      </>
    );
  }
  if (feed.status === "error" && !feed.data) {
    return (
      <>
        <ViewHead sup={`PR · ${id}`} title={id} sub="" />
        <div class="op-error">
          Failed to load PR: {feed.error ?? "unknown error"}
        </div>
      </>
    );
  }
  if (!feed.data) return null;
  const pr = feed.data;
  return (
    <>
      <ViewHead
        sup={`Traces · ${pr.ticket_id} · PR #${pr.pr_number}`}
        title={`PR #${pr.pr_number}`}
        sub={`${pr.repo_full_name} · head ${pr.head_sha.slice(0, 8)} · opened ${pr.opened_at.slice(0, 10)}`}
        right={
          <div style={{ display: "flex", gap: "8px" }}>
            {pr.ticket_id && (
              <a
                href={href({ name: "trace-detail", id: pr.ticket_id })}
              >
                <Button size="sm" variant="ghost">
                  Trace →
                </Button>
              </a>
            )}
            {pr.pr_url && (
              <a href={pr.pr_url} target="_blank" rel="noopener noreferrer">
                <Button size="sm">Open on GitHub ↗</Button>
              </a>
            )}
          </div>
        }
      />

      <div class="op-meta-row">
        <Pill tone={pr.merged ? "ok" : "active"}>
          {pr.merged ? "merged" : "open"}
        </Pill>
        {pr.first_pass_accepted && <Pill tone="ok">first-pass</Pill>}
        <Pill tone="cool">profile · {pr.client_profile || "—"}</Pill>
        <Pill tone="cool">{pr.commits.length} commits</Pill>
        <Pill tone="cool">{pr.issues.length} issues</Pill>
        <Pill tone="cool">{pr.matches.length} lesson matches</Pill>
      </div>

      <section class="op-section">
        <SectionHeader
          label="CI checks"
          right={
            pr.ci_checks_available
              ? "from L3 review ingestion"
              : "CI ingestion not wired — see docs"
          }
        />
        {pr.ci_checks_available ? (
          <div class="op-empty">CI data would render here.</div>
        ) : (
          <div class="op-empty">
            CI check status is not persisted to autonomy.db yet. Plan
            flagged this as a known gap; this card stays hollow until
            L3 CI ingestion lands.
          </div>
        )}
      </section>

      <div class="op-pr-two-col">
        <section>
          <SectionHeader
            label="Issues raised by L3 review"
            right={`${pr.issues.length} total`}
          />
          <IssuesTable rows={pr.issues} />
        </section>
        <section>
          <SectionHeader label="Lesson matches" right={`${pr.matches.length}`} />
          <MatchesTable rows={pr.matches} />
        </section>
      </div>

      {pr.auto_merge && <AutoMergeCard decision={pr.auto_merge} />}
    </>
  );
}

function IssuesTable({ rows }: { rows: PRReviewIssue[] }) {
  return (
    <Table<PRReviewIssue>
      rowKey={(r) => String(r.id)}
      rows={rows}
      empty="No review issues raised."
      columns={[
        {
          key: "sev",
          label: "Sev",
          width: "80px",
          render: (r) => (
            <Pill tone={SEVERITY_TONE[r.severity] ?? "cool"}>{r.severity}</Pill>
          ),
        },
        {
          key: "where",
          label: "Where",
          render: (r) => (
            <span>
              <span class="mono">
                {r.where}
                {r.line_start ? `:${r.line_start}` : ""}
              </span>
              <br />
              <span style={{ color: "var(--ink-700)" }}>{r.summary}</span>
            </span>
          ),
        },
        {
          key: "matched",
          label: "Matched",
          width: "100px",
          numeric: true,
          render: (r) =>
            r.matched ? (
              <span class="mono">
                {Math.round(r.matched.confidence * 100)}%
              </span>
            ) : (
              <span style={{ color: "var(--ink-500)" }}>—</span>
            ),
        },
      ]}
    />
  );
}

function MatchesTable({ rows }: { rows: PRLessonMatch[] }) {
  return (
    <Table<PRLessonMatch>
      rowKey={(m) => m.lesson_id}
      rows={rows}
      empty="No lesson matches."
      columns={[
        {
          key: "lesson",
          label: "Lesson",
          render: (m) => <span class="mono">{m.lesson_id}</span>,
        },
        {
          key: "state",
          label: "State",
          width: "110px",
          render: (m) => (
            <Pill tone={m.applied ? "ok" : m.status === "approved" ? "active" : "cool"}>
              {m.status || "—"}
            </Pill>
          ),
        },
      ]}
    />
  );
}

function AutoMergeCard({
  decision,
}: {
  decision: NonNullable<PRDetailResponse["auto_merge"]>;
}) {
  const cls = `op-auto-merge-decision is-${decision.decision.toLowerCase()}`;
  const gates = decision.gates ?? {};
  const gateEntries = Object.entries(gates);
  return (
    <section class="op-section">
      <SectionHeader label="Auto-merge decision" />
      <div class="op-auto-merge-card">
        <span class={cls}>{decision.decision || "—"}</span>
        {decision.confidence !== null && (
          <span class="op-auto-merge-conf">
            confidence · {(decision.confidence * 100).toFixed(0)}%
          </span>
        )}
        {decision.reason && (
          <span style={{ color: "var(--ink-700)" }}>{decision.reason}</span>
        )}
        {gateEntries.length > 0 && (
          <ul class="op-auto-merge-reasons">
            {gateEntries.map(([k, v]) => (
              <li key={k}>
                <span
                  style={{ color: v ? "var(--signal-ok)" : "var(--signal-err)" }}
                >
                  {v ? "✓" : "✗"}
                </span>{" "}
                {k}
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
