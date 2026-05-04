import { useCallback, useEffect, useMemo, useState } from "preact/hooks";
import { ViewHead } from "../chrome";
import { Button, Chip, Pill, SectionHeader, Table } from "../primitives";
import type { PillTone } from "../primitives";
import { fetchHeaders } from "../api/key";
import { useFeed } from "../hooks/useFeed";
import { useLiveLog } from "../hooks/useLiveLog";
import type { LiveLogEntry } from "../hooks/useLiveLog";
import type {
  AgentRosterEntry,
  AgentRosterResponse,
  ActivitySummaryResponse,
  ActivitySummaryItem,
  TraceStatus,
  TraceSummary,
  TracesResponse,
} from "../api/types";
import { href } from "../router";

type StatusFilter = TraceStatus | "all";
type LiveFilter = "all" | "team_lead" | "dev" | "review" | "qa" | "other";
const PAGE_SIZE = 200;

const FILTERS: readonly { label: string; value: StatusFilter }[] = [
  { label: "All", value: "all" },
  { label: "In-flight", value: "in-flight" },
  { label: "Stuck", value: "stuck" },
  { label: "Queued", value: "queued" },
  { label: "Done", value: "done" },
  { label: "Hidden", value: "hidden" },
];

const STATUS_TONE: Record<TraceStatus, PillTone> = {
  "in-flight": "active",
  stuck: "warn",
  queued: "cool",
  done: "ok",
  hidden: "err",
};

const LIVE_FILTERS: readonly { label: string; value: LiveFilter }[] = [
  { label: "All", value: "all" },
  { label: "Team Lead", value: "team_lead" },
  { label: "Devs", value: "dev" },
  { label: "Review", value: "review" },
  { label: "QA", value: "qa" },
  { label: "Other", value: "other" },
];

export function TicketsView() {
  const [filter, setFilter] = useState<StatusFilter>("in-flight");
  const [offset, setOffset] = useState(0);
  const [includeHidden, setIncludeHidden] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const statusQuery = filter === "all" ? "" : `&status=${encodeURIComponent(filter)}`;
  const feed = useFeed<TracesResponse>(
    `/api/operator/traces?limit=${PAGE_SIZE}&offset=${offset}&include_hidden=${includeHidden ? "true" : "false"}${statusQuery}`,
    { clearOnUrlChange: true },
  );

  const counts = useMemo(() => {
    const base: Record<StatusFilter, number> = {
      all: 0,
      "in-flight": 0,
      stuck: 0,
      queued: 0,
      done: 0,
      hidden: 0,
    };
    if (feed.data) Object.assign(base, feed.data.status_counts);
    return base;
  }, [feed.data]);

  const selectedRow = useMemo(
    () => feed.data?.traces.find((t) => t.id === selected) ?? null,
    [feed.data, selected],
  );

  // Auto-select first in-flight row so the rail renders live data on load.
  // MUST be in an effect, not a render-body setState call (that triggers a
  // Preact "cannot update during render" warning and re-render loop).
  useEffect(() => {
    if (!feed.data) return;
    if (selected && feed.data.traces.some((t) => t.id === selected)) return;
    const firstLive = feed.data.traces.find((t) => t.status === "in-flight");
    const firstRow = firstLive ?? feed.data.traces[0];
    if (firstRow) setSelected(firstRow.id);
  }, [feed.data, selected]);

  return (
    <>
      <ViewHead
        sup="Pipeline · tickets"
        title="Tickets"
        sub="Live pipeline board. Click a row to see its live log."
        rnum={String(counts["in-flight"])}
        rlabel="In-flight"
      />

      <div style={{ display: "flex", gap: "8px", marginBottom: "20px", flexWrap: "wrap" }}>
        {FILTERS.map((f) => (
          f.value === "hidden" && !includeHidden ? null : (
            <Chip
              key={f.value}
              label={f.label}
              count={counts[f.value]}
              on={filter === f.value}
              onClick={() => {
                setFilter(f.value);
                setOffset(0);
                setSelected(null);
              }}
            />
          )
        ))}
        <Button
          size="sm"
          variant={includeHidden ? "danger" : "default"}
          onClick={() => {
            setIncludeHidden((v) => !v);
            if (filter === "hidden") setFilter("all");
            setOffset(0);
            setSelected(null);
          }}
        >
          {includeHidden ? "Hide hidden" : "Show hidden"}
        </Button>
      </div>

      <div class="op-tickets-grid" style={{ border: "var(--rule)" }}>
        <div class="op-tickets-list">
          {feed.status === "loading" && !feed.data && (
            <div class="op-loading">Loading tickets…</div>
          )}
          {feed.status === "error" && (
            <div class="op-error">Failed to load: {feed.error}</div>
          )}
          {feed.data && (
            <>
              <div class="op-table-tools">
                <span class="op-muted">
                  Showing {feed.data.traces.length} of {feed.data.count} · offset{" "}
                  {feed.data.offset}
                </span>
                <div style={{ display: "flex", gap: "8px" }}>
                  <Button
                    size="sm"
                    disabled={offset === 0}
                    onClick={() => {
                      setOffset(Math.max(0, offset - PAGE_SIZE));
                      setSelected(null);
                    }}
                  >
                    Previous
                  </Button>
                  <Button
                    size="sm"
                    disabled={offset + PAGE_SIZE >= feed.data.count}
                    onClick={() => {
                      setOffset(offset + PAGE_SIZE);
                      setSelected(null);
                    }}
                  >
                    Next
                  </Button>
                </div>
              </div>
              <Table<TraceSummary>
                rowKey={(t) => t.id}
                rows={feed.data.traces}
                isLive={(t) => t.status === "in-flight"}
                onRowClick={(t) => setSelected(t.id)}
                empty={`No ${filter === "all" ? "tickets" : filter + " tickets"} right now.`}
                columns={[
                  {
                    key: "id",
                    label: "Ticket",
                    width: "120px",
                    render: (t) => (
                      <span
                        class="op-mono"
                        style={{
                          color:
                            t.id === selected
                              ? "var(--accent)"
                              : "var(--ink-700)",
                        }}
                      >
                        {t.id}
                      </span>
                    ),
                  },
                  {
                    key: "title",
                    label: "Title",
                    render: (t) => t.title || "—",
                  },
                  {
                    key: "status",
                    label: "Status",
                    width: "120px",
                    render: (t) => (
                      <Pill tone={STATUS_TONE[t.status]}>{t.raw_status}</Pill>
                    ),
                  },
                  {
                    key: "phase",
                    label: "Phase",
                    width: "120px",
                    render: (t) => (
                      <span class="op-mono">{t.phase || "—"}</span>
                    ),
                  },
                  {
                    key: "elapsed",
                    label: "Elapsed",
                    width: "80px",
                    numeric: true,
                    render: (t) => t.elapsed || "—",
                  },
                ]}
              />
            </>
          )}
        </div>

        <aside class="op-tickets-rail">
          <TicketRail row={selectedRow} />
        </aside>
      </div>
    </>
  );
}

function TicketRail({ row }: { row: TraceSummary | null }) {
  const log = useLiveLog(row?.id ?? null);
  const roster = useFeed<AgentRosterResponse>(
    row ? `/api/operator/tickets/${encodeURIComponent(row.id)}/agents` : null,
    { clearOnUrlChange: true },
  );
  const activitySummary = useFeed<ActivitySummaryResponse>(
    row ? `/api/operator/tickets/${encodeURIComponent(row.id)}/activity-summary` : null,
    { clearOnUrlChange: true },
  );
  const [triggerState, setTriggerState] = useState<"idle" | "busy" | "done" | "error">("idle");
  const [liveFilter, setLiveFilter] = useState<LiveFilter>("all");

  const visibleEntries = useMemo(
    () =>
      liveFilter === "all"
        ? log.entries
        : log.entries.filter((entry) => entry.role_group === liveFilter),
    [log.entries, liveFilter],
  );

  // Reset button state when selected ticket changes
  useEffect(() => {
    setTriggerState("idle");
    setLiveFilter("all");
  }, [row?.id]);

  const removeTrigger = useCallback(async () => {
    if (!row || triggerState === "busy") return;
    setTriggerState("busy");
    try {
      const res = await fetch(
        `/api/operator/tickets/${encodeURIComponent(row.id)}/trigger-label`,
        {
          method: "DELETE",
          headers: fetchHeaders({ Accept: "application/json" }),
          credentials: "same-origin",
        },
      );
      setTriggerState(res.ok ? "done" : "error");
    } catch {
      setTriggerState("error");
    }
  }, [row, triggerState]);

  if (!row) {
    return <div class="op-rail-empty">Select a ticket to see its live log</div>;
  }

  return (
    <>
      <div>
        <span class="op-rail-ticket-id">{row.id}</span>
        <br />
        <span class="op-rail-ticket-title">{row.title || "—"}</span>
      </div>

      <div style={{ display: "flex", gap: "6px", flexWrap: "wrap" }}>
        <Pill tone={STATUS_TONE[row.status]}>{row.raw_status}</Pill>
        {row.phase && <Pill tone="cool">phase · {row.phase}</Pill>}
      </div>

      <div style={{ display: "flex", gap: "6px", flexWrap: "wrap" }}>
        <a href={href({ name: "trace-detail", id: row.id })}>
          <Button size="sm" variant="ghost">
            Trace →
          </Button>
        </a>
        {row.pr_url && (
          <a href={row.pr_url} target="_blank" rel="noopener noreferrer">
            <Button size="sm" variant="ghost">
              PR ↗
            </Button>
          </a>
        )}
        {(row.status === "in-flight" || row.status === "stuck") && (
          <Button
            size="sm"
            variant="ghost"
            onClick={removeTrigger}
            disabled={triggerState === "busy" || triggerState === "done"}
          >
            {triggerState === "busy"
              ? "Removing…"
              : triggerState === "done"
                ? "Trigger removed"
                : triggerState === "error"
                  ? "Failed — retry?"
                  : "Remove Trigger"}
          </Button>
        )}
      </div>

      <TeamActivity agents={roster.data?.agents} state={roster.status} compact />

      {row.status !== "in-flight" && (
        <ActivitySummaryPanel
          data={activitySummary.data}
          state={activitySummary.status}
          compact
        />
      )}

      <SectionHeader
        label="Live log"
        right={<ConnBadge state={log.state} />}
      />
      <div class="op-live-filter-row">
        {LIVE_FILTERS.map((f) => (
          <Chip
            key={f.value}
            label={f.label}
            on={liveFilter === f.value}
            onClick={() => setLiveFilter(f.value)}
          />
        ))}
      </div>
      <div class="op-rail-live-log">
        {visibleEntries.length === 0 && (
          <div class="op-rail-log-conn">
            {log.state === "connecting"
              ? "Connecting…"
              : log.state === "connected"
                ? log.entries.length === 0
                  ? "Waiting for activity…"
                  : "No activity for this filter."
                : log.state === "error"
                  ? log.error ?? "Disconnected"
                  : "Idle"}
          </div>
        )}
        {visibleEntries.map((e, i) => (
          <LogLine key={`${e.event_id ?? e.timestamp}-${i}`} entry={e} />
        ))}
      </div>
    </>
  );
}

function ConnBadge({ state }: { state: "idle" | "connecting" | "connected" | "error" }) {
  if (state === "connected") return <Pill tone="ok">live</Pill>;
  if (state === "connecting") return <Pill tone="cool">connecting</Pill>;
  if (state === "error") return <Pill tone="err">reconnecting</Pill>;
  return <Pill tone="cool">idle</Pill>;
}

function LogLine({ entry }: { entry: LiveLogEntry }) {
  const message =
    entry.kind === "tool_use"
      ? `${entry.tool_name ?? ""} ${entry.description ?? ""}`.trim()
      : entry.kind === "text"
        ? entry.text ?? ""
        : `${entry.kind}`;

  return (
    <div class="op-rail-log-line">
      <span class="op-rail-log-time">{formatLogTime(entry.observed_at || entry.timestamp)}</span>
      <span class="op-rail-log-team">{entry.display_name || entry.teammate || "—"}</span>
      <span class="op-rail-log-msg" title={message}>
        {message}
      </span>
    </div>
  );
}

export function ActivitySummaryPanel({
  data,
  state,
  compact = false,
}: {
  data: ActivitySummaryResponse | undefined;
  state: string;
  compact?: boolean;
}) {
  const [actorFilter, setActorFilter] = useState<string>("all");

  useEffect(() => {
    setActorFilter("all");
  }, [data?.ticket_id]);

  if (state === "loading" && !data) {
    return <div class="op-rail-log-conn">Loading activity summary...</div>;
  }
  if (!data || data.raw_event_count === 0) {
    return <div class="op-rail-log-conn">No finished activity summary available.</div>;
  }
  const filteredHighlights =
    actorFilter === "all"
      ? data.highlights
      : data.highlights.filter((item) => item.teammate === actorFilter);
  const topHighlights = filteredHighlights.slice(-(compact ? 6 : 12));
  const visibleTeammates =
    actorFilter === "all"
      ? data.teammates
      : data.teammates.filter((teammate) => teammate.teammate === actorFilter);
  const filteredWarnings =
    actorFilter === "all"
      ? data.warnings
      : visibleTeammates.flatMap((teammate) => teammate.warnings);
  const visibleRawCount =
    actorFilter === "all"
      ? data.raw_event_count
      : visibleTeammates.reduce((sum, teammate) => sum + teammate.raw_event_count, 0);
  const visibleUniqueCount =
    actorFilter === "all"
      ? data.deduped_event_count
      : visibleTeammates.reduce((sum, teammate) => sum + teammate.deduped_event_count, 0);
  return (
    <section class={compact ? "op-activity-summary is-compact" : "op-activity-summary"}>
      <SectionHeader
        label="Activity summary"
        right={`${visibleRawCount} raw · ${visibleUniqueCount} unique`}
      />
      <div class="op-summary-text">{data.summary}</div>
      {data.teammates.length > 1 && (
        <div class="op-summary-actor-strip">
          <button
            type="button"
            class={actorFilter === "all" ? "is-active" : ""}
            onClick={() => setActorFilter("all")}
          >
            All {data.deduped_event_count}
          </button>
          {data.teammates.map((teammate) => (
            <button
              key={teammate.teammate}
              type="button"
              class={actorFilter === teammate.teammate ? "is-active" : ""}
              onClick={() => {
                setActorFilter((current) =>
                  current === teammate.teammate ? "all" : teammate.teammate,
                );
              }}
            >
              {teammate.display_name} {teammate.deduped_event_count}
            </button>
          ))}
        </div>
      )}
      {filteredWarnings.length > 0 && (
        <div class="op-summary-warnings">
          {filteredWarnings.slice(0, compact ? 3 : 8).map((warning, i) => (
            <div key={`${warning}-${i}`}>{warning}</div>
          ))}
        </div>
      )}
      <div class="op-summary-list">
        {topHighlights.length === 0 ? (
          <div class="op-rail-log-conn">No summary items for this teammate.</div>
        ) : (
          topHighlights.map((item) => (
            <SummaryItem key={item.event_id || `${item.display_name}-${item.message}`} item={item} />
          ))
        )}
      </div>
      {!compact && (
        <div class="op-summary-teammates">
          {visibleTeammates.map((teammate) => (
            <div key={teammate.teammate} class="op-summary-teammate">
              <div class="op-summary-teammate-head">
                <span>{teammate.display_name}</span>
                <span>
                  {teammate.raw_event_count} raw · {teammate.deduped_event_count} unique
                </span>
              </div>
              <div class="op-summary-tool-row">
                {teammate.tools.slice(0, 6).map((tool) => (
                  <span key={tool.name}>{tool.name} {tool.count}</span>
                ))}
              </div>
              {teammate.actions.slice(0, 5).map((action) => (
                <SummaryItem key={action.event_id || action.message} item={action} />
              ))}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function SummaryItem({ item }: { item: ActivitySummaryItem }) {
  return (
    <div class="op-summary-item">
      <span class="op-summary-item-team">{item.display_name || item.teammate}</span>
      <span class="op-summary-item-msg" title={item.message}>
        {item.message}
        {item.count > 1 && <span class="op-summary-repeat"> x{item.count}</span>}
      </span>
    </div>
  );
}

export function TeamActivity({
  agents,
  state,
  compact = false,
}: {
  agents: readonly AgentRosterEntry[] | undefined;
  state: string;
  compact?: boolean;
}) {
  if (state === "loading" && !agents) {
    return <div class="op-rail-log-conn">Loading team activity...</div>;
  }
  if (!agents || agents.length === 0) {
    return <div class="op-rail-log-conn">No agents spawned for this ticket.</div>;
  }
  return (
    <div class={compact ? "op-team-activity is-compact" : "op-team-activity"}>
      {agents.map((agent) => (
        <AgentActivityCard key={agent.teammate} agent={agent} compact={compact} />
      ))}
    </div>
  );
}

function AgentActivityCard({
  agent,
  compact,
}: {
  agent: AgentRosterEntry;
  compact: boolean;
}) {
  const tone: PillTone =
    agent.state === "running"
      ? "active"
      : agent.state === "idle"
        ? "cool"
        : "warn";
  return (
    <div class="op-agent-card">
      <div class="op-agent-card-head">
        <span class="op-agent-name">{agent.display_name || agent.teammate}</span>
        <Pill tone={tone}>{agent.state}</Pill>
      </div>
      <div class="op-agent-meta">
        <span>{agent.last_at ? formatLogTime(agent.last_at) : "—"}</span>
        {agent.last_tool && <span>{agent.last_tool}</span>}
        <span>{agent.tool_uses} tools</span>
        {agent.total_tokens > 0 && <span>{formatCompactNumber(agent.total_tokens)} tok</span>}
      </div>
      <div class="op-agent-current" title={agent.current_activity || agent.last_summary}>
        {agent.current_activity || agent.last_summary || "No displayable activity yet."}
      </div>
      {!compact && agent.latest_events.length > 0 && (
        <div class="op-agent-events">
          {agent.latest_events.slice(-3).map((event) => (
            <div key={event.event_id} class="op-agent-event">
              <span>{formatLogTime(event.observed_at || event.timestamp)}</span>
              <span>{eventMessage(event)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function eventMessage(entry: {
  kind: string;
  tool_name?: string;
  description?: string;
  text?: string;
  summary?: string;
}): string {
  if (entry.kind === "tool_use") {
    return `${entry.tool_name ?? ""} ${entry.description ?? ""}`.trim();
  }
  if (entry.kind === "text") return entry.text ?? "";
  if (entry.kind === "task_started") return `started ${entry.description ?? ""}`.trim();
  if (entry.kind === "task_notification") return `done ${String(entry.summary ?? "")}`.trim();
  return entry.kind;
}

function formatCompactNumber(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}m`;
  if (value >= 1_000) return `${Math.round(value / 100) / 10}k`;
  return String(value);
}

function formatLogTime(ts: string): string {
  if (!ts) return "—";
  const m = /T(\d\d:\d\d:\d\d)/.exec(ts);
  return m ? (m[1] ?? ts) : ts;
}
