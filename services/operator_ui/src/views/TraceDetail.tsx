import { useState } from "preact/hooks";
import { ViewHead } from "../chrome";
import { Button, PhaseDots, Pill, SectionHeader } from "../primitives";
import { fetchHeaders } from "../api/key";
import type { PhaseState, PillTone } from "../primitives";
import { useFeed } from "../hooks/useFeed";
import type {
  ActivitySummaryResponse,
  AgentRosterResponse,
  TraceDetailResponse,
  TracePhase,
  TraceStatus,
} from "../api/types";
import { ActivitySummaryPanel, TeamActivity } from "./Tickets";
import { readableErrorText } from "./actionFeedback";
import type { ActionNotice } from "./actionFeedback";

const STATUS_TONE: Record<TraceStatus, PillTone> = {
  "in-flight": "active",
  stuck: "warn",
  queued: "cool",
  done: "ok",
  hidden: "err",
};
type TraceLifecycleAction = "suppressed" | "misfire" | "stale" | "open";

interface Props {
  id: string;
}

export function TraceDetailView({ id }: Props) {
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<ActionNotice | null>(null);
  const feed = useFeed<TraceDetailResponse>(
    `/api/operator/traces/${encodeURIComponent(id)}`,
    { clearOnUrlChange: true },
  );
  const roster = useFeed<AgentRosterResponse>(
    `/api/operator/tickets/${encodeURIComponent(id)}/agents`,
    { clearOnUrlChange: true },
  );
  const activitySummary = useFeed<ActivitySummaryResponse>(
    `/api/operator/tickets/${encodeURIComponent(id)}/activity-summary`,
    { clearOnUrlChange: true },
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
            <Button
              size="sm"
              disabled={busy}
              onClick={() => {
                void markTrace(
                  id,
                  t.hidden ? "open" : "suppressed",
                  feed.refresh,
                  setBusy,
                  setNotice,
                );
              }}
            >
              {t.hidden ? "Restore" : "Hide"}
            </Button>
            {!t.hidden && (
              <Button
                size="sm"
                variant="danger"
                disabled={busy}
                onClick={() => {
                  void markTrace(id, "misfire", feed.refresh, setBusy, setNotice);
                }}
              >
                Misfire
              </Button>
            )}
            {!t.hidden && t.lifecycle_state === "stale" && (
              <Button
                size="sm"
                disabled={busy}
                onClick={() => {
                  void markTrace(id, "open", feed.refresh, setBusy, setNotice);
                }}
              >
                Mark Active
              </Button>
            )}
            {!t.hidden && t.lifecycle_state !== "stale" && t.status !== "done" && (
              <Button
                size="sm"
                disabled={busy}
                onClick={() => {
                  void markTrace(id, "stale", feed.refresh, setBusy, setNotice);
                }}
              >
                Stale
              </Button>
            )}
          </div>
        }
      />

      <div class="op-meta-row">
        <Pill tone={STATUS_TONE[t.status]}>{t.raw_status}</Pill>
        {t.state_reason && (
          <span title={t.state_reason}>
            <Pill tone="warn">state · {t.state_reason.slice(0, 48)}</Pill>
          </span>
        )}
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

      {notice && (
        <div class={`op-action-notice is-${notice.tone}`} role="status">
          {notice.text}
        </div>
      )}

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
        <ActivitySummaryPanel
          data={activitySummary.data}
          state={activitySummary.status}
          error={activitySummary.error}
        />
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
        <TeamActivity
          state={roster.status}
          error={roster.error}
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

async function markTrace(
  ticketId: string,
  state: TraceLifecycleAction,
  refresh: () => void,
  setBusy: (value: boolean) => void,
  setNotice: (notice: ActionNotice) => void,
) {
  const fallbackReason =
    state === "misfire"
      ? "Marked as misfire from operator dashboard"
      : state === "suppressed"
        ? "Hidden from operator dashboard"
        : state === "stale"
          ? "Marked stale from operator dashboard"
          : "Restored from operator dashboard";
  const reason = window.prompt("Reason", fallbackReason) ?? "";
  if (!reason.trim()) return;
  setBusy(true);
  setNotice({ tone: "warn", text: `Updating ${ticketId}...` });
  try {
    const res = await fetch(
      `/api/operator/traces/${encodeURIComponent(ticketId)}/state`,
      {
        method: "POST",
        headers: fetchHeaders({
          Accept: "application/json",
          "Content-Type": "application/json",
        }),
        credentials: "same-origin",
        body: JSON.stringify({
          state,
          reason,
          exclude_metrics: state === "misfire" || state === "suppressed",
        }),
      },
    );
    if (!res.ok) {
      const text = await res.text();
      throw new Error(`${res.status}: ${readableErrorText(text)}`);
    }
    setNotice({ tone: "ok", text: `Updated ${ticketId}.` });
    refresh();
  } catch (err) {
    const detail = err instanceof Error ? err.message : String(err);
    setNotice({ tone: "err", text: `Failed to update ${ticketId}: ${detail}` });
  } finally {
    setBusy(false);
  }
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
