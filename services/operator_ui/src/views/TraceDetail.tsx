import { ViewHead } from "../chrome";
import { Button, PhaseDots, Pill, SectionHeader } from "../primitives";
import type { PhaseState, PillTone } from "../primitives";
import { useFeed } from "../hooks/useFeed";
import type {
  AgentRosterEntry,
  AgentRosterResponse,
  TraceDetailResponse,
  TracePhase,
  TraceStatus,
} from "../api/types";

const STATUS_TONE: Record<TraceStatus, PillTone> = {
  "in-flight": "active",
  stuck: "warn",
  queued: "cool",
  done: "ok",
};

interface Props {
  id: string;
}

export function TraceDetailView({ id }: Props) {
  const feed = useFeed<TraceDetailResponse>(
    `/api/operator/traces/${encodeURIComponent(id)}`,
  );
  const roster = useFeed<AgentRosterResponse>(
    `/api/operator/tickets/${encodeURIComponent(id)}/agents`,
  );

  if (feed.status === "loading" && !feed.data) {
    return (
      <>
        <ViewHead sup={`Traces · ${id}`} title={id} sub="Loading…" />
        <div class="op-loading">Fetching trace…</div>
      </>
    );
  }

  if (feed.status === "error" && !feed.data) {
    return (
      <>
        <ViewHead sup={`Traces · ${id}`} title={id} sub="" />
        <div class="op-error">
          Failed to load trace: {feed.error ?? "unknown error"}
        </div>
      </>
    );
  }

  if (!feed.data) return null;
  const t = feed.data;
  const dotStates: PhaseState[] = t.phases.map((p) => p.state);

  return (
    <>
      <ViewHead
        sup={`Traces · ${id}`}
        title={t.title || id}
        sub={`Run started ${t.started_at || "—"} · elapsed ${t.elapsed || "—"}`}
        right={
          <div style={{ display: "flex", gap: "8px", alignItems: "center" }}>
            {t.pr_url && (
              <a href={t.pr_url} target="_blank" rel="noopener noreferrer">
                <Button size="sm" variant="ghost">
                  Open PR ↗
                </Button>
              </a>
            )}
          </div>
        }
      />

      <div class="op-meta-row">
        <Pill tone={STATUS_TONE[t.status]}>{t.raw_status}</Pill>
        {t.pipeline_mode && (
          <Pill tone="cool">mode · {t.pipeline_mode}</Pill>
        )}
        {t.review_verdict && (
          <Pill tone={t.review_verdict === "approved" ? "ok" : "warn"}>
            review · {t.review_verdict}
          </Pill>
        )}
        {t.qa_result && (
          <Pill tone={t.qa_result === "pass" ? "ok" : "warn"}>
            qa · {t.qa_result}
          </Pill>
        )}
      </div>

      <section class="op-section">
        <SectionHeader
          label="Phase timeline"
          right={<PhaseDots phases={dotStates} />}
        />
        <div class="op-phase-timeline">
          {t.phases.map((p, i) => (
            <PhaseRow key={p.key} phase={p} index={i} />
          ))}
        </div>
      </section>

      <section class="op-section">
        <SectionHeader
          label="Session panels"
          right={
            roster.data
              ? `${roster.data.agents.length} teammate${roster.data.agents.length === 1 ? "" : "s"}`
              : roster.status.toUpperCase()
          }
        />
        <AgentRoster
          state={roster.status}
          agents={roster.data?.agents}
        />
      </section>

      <section class="op-section">
        <SectionHeader
          label="Raw events"
          right={`${t.events.length} · newest last`}
        />
        <div class="op-event-log">
          {t.events.map((e, i) => (
            <div key={i} class="op-event-row">
              <span class="op-event-time">{formatTime(e.t)}</span>
              <span class="op-event-ev">{e.ev}</span>
              <span class="op-event-msg" title={e.msg}>
                {e.msg || "—"}
              </span>
            </div>
          ))}
        </div>
      </section>
    </>
  );
}

function PhaseRow({ phase, index }: { phase: TracePhase; index: number }) {
  return (
    <div class={`op-phase-row is-${phase.state}`}>
      <span class="op-phase-dur">
        {phase.duration_seconds > 0
          ? formatDuration(phase.duration_seconds)
          : "—"}
      </span>
      <span class="op-phase-idx">{index + 1}.</span>
      <span>
        <PhaseDotCompact state={phase.state} />
      </span>
      <span class="op-phase-name">{phase.name}</span>
      <span class="op-phase-events">
        {phase.event_count > 0 ? `${phase.event_count} events` : "—"}
      </span>
      <span class="op-phase-state">{phase.state}</span>
    </div>
  );
}

function PhaseDotCompact({ state }: { state: PhaseState }) {
  const color =
    state === "done"
      ? "var(--signal-ok)"
      : state === "active"
        ? "var(--signal-active)"
        : state === "fail"
          ? "var(--signal-err)"
          : "var(--ink-400)";
  return (
    <span
      style={{
        display: "inline-block",
        width: "8px",
        height: "8px",
        borderRadius: "50%",
        background: color,
        boxShadow: state === "active" ? `0 0 8px ${color}` : "none",
      }}
    />
  );
}

function formatDuration(seconds: number): string {
  const s = Math.round(seconds);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  if (m < 60) return `${m}m ${rem.toString().padStart(2, "0")}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${(m % 60).toString().padStart(2, "0")}m`;
}

function formatTime(iso: string): string {
  if (!iso) return "—";
  // "2026-04-18T12:00:42+00:00" → "12:00:42"
  const match = /T(\d\d:\d\d:\d\d)/.exec(iso);
  return match ? (match[1] ?? iso) : iso;
}

function AgentRoster({
  state,
  agents,
}: {
  state: string;
  agents: readonly AgentRosterEntry[] | undefined;
}) {
  if (state === "loading" && !agents) {
    return <div class="op-loading">Loading roster…</div>;
  }
  if (!agents || agents.length === 0) {
    return <div class="op-empty">No agents spawned for this ticket.</div>;
  }
  return (
    <table class="op-tbl">
      <thead>
        <tr>
          <th style={{ width: "160px" }}>Teammate</th>
          <th style={{ width: "100px" }}>State</th>
          <th>Last activity</th>
        </tr>
      </thead>
      <tbody>
        {agents.map((a) => {
          const tone: PillTone =
            a.state === "running"
              ? "active"
              : a.state === "idle"
                ? "cool"
                : "warn";
          return (
            <tr key={a.teammate}>
              <td class="mono">{a.teammate}</td>
              <td>
                <Pill tone={tone}>{a.state}</Pill>
              </td>
              <td class="mono">{a.last_at ? formatTime(a.last_at) : "—"}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
