import { useCallback, useEffect, useMemo, useState } from "preact/hooks";
import { ViewHead } from "../chrome";
import { Button, Chip, Pill, SectionHeader, Table } from "../primitives";
import type { PillTone } from "../primitives";
import { useFeed } from "../hooks/useFeed";
import { useLiveLog } from "../hooks/useLiveLog";
import type { LiveLogEntry } from "../hooks/useLiveLog";
import type {
  AgentRosterResponse,
  TraceStatus,
  TraceSummary,
  TracesResponse,
} from "../api/types";
import { href } from "../router";

type StatusFilter = TraceStatus | "all";

const FILTERS: readonly { label: string; value: StatusFilter }[] = [
  { label: "All", value: "all" },
  { label: "In-flight", value: "in-flight" },
  { label: "Stuck", value: "stuck" },
  { label: "Queued", value: "queued" },
  { label: "Done", value: "done" },
];

const STATUS_TONE: Record<TraceStatus, PillTone> = {
  "in-flight": "active",
  stuck: "warn",
  queued: "cool",
  done: "ok",
};

export function TicketsView() {
  const [filter, setFilter] = useState<StatusFilter>("in-flight");
  const [selected, setSelected] = useState<string | null>(null);
  const feed = useFeed<TracesResponse>("/api/operator/traces?limit=200");

  const rows = useMemo(() => {
    if (!feed.data) return [];
    if (filter === "all") return feed.data.traces;
    return feed.data.traces.filter((t) => t.status === filter);
  }, [feed.data, filter]);

  const counts = useMemo(() => {
    const base: Record<StatusFilter, number> = {
      all: 0,
      "in-flight": 0,
      stuck: 0,
      queued: 0,
      done: 0,
    };
    if (feed.data) {
      base.all = feed.data.traces.length;
      for (const t of feed.data.traces) base[t.status] += 1;
    }
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
    if (selected || !feed.data) return;
    const firstLive = feed.data.traces.find((t) => t.status === "in-flight");
    if (firstLive) setSelected(firstLive.id);
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
          <Chip
            key={f.value}
            label={f.label}
            count={counts[f.value]}
            on={filter === f.value}
            onClick={() => setFilter(f.value)}
          />
        ))}
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
            <Table<TraceSummary>
              rowKey={(t) => t.id}
              rows={rows}
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
  );
  const [triggerState, setTriggerState] = useState<"idle" | "busy" | "done" | "error">("idle");

  // Reset button state when selected ticket changes
  useEffect(() => { setTriggerState("idle"); }, [row?.id]);

  const removeTrigger = useCallback(async () => {
    if (!row || triggerState === "busy") return;
    setTriggerState("busy");
    try {
      const res = await fetch(
        `/api/operator/tickets/${encodeURIComponent(row.id)}/trigger-label`,
        { method: "DELETE" },
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

      {roster.data && roster.data.agents.length > 0 && (
        <div style={{ display: "flex", gap: "6px", flexWrap: "wrap" }}>
          {roster.data.agents.map((a) => (
            <Pill
              key={a.teammate}
              tone={a.state === "running" ? "active" : a.state === "idle" ? "cool" : "warn"}
            >
              {a.teammate}
            </Pill>
          ))}
        </div>
      )}

      <SectionHeader
        label="Live log"
        right={<ConnBadge state={log.state} />}
      />
      <div class="op-rail-live-log">
        {log.entries.length === 0 && (
          <div class="op-rail-log-conn">
            {log.state === "connecting"
              ? "Connecting…"
              : log.state === "connected"
                ? "Waiting for activity…"
                : log.state === "error"
                  ? log.error ?? "Disconnected"
                  : "Idle"}
          </div>
        )}
        {log.entries.map((e, i) => (
          <LogLine key={`${e.timestamp}-${i}`} entry={e} />
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
      <span class="op-rail-log-time">{formatLogTime(entry.timestamp)}</span>
      <span class="op-rail-log-team">{entry.teammate || "—"}</span>
      <span class="op-rail-log-msg" title={message}>
        {message}
      </span>
    </div>
  );
}

function formatLogTime(ts: string): string {
  if (!ts) return "—";
  const m = /T(\d\d:\d\d:\d\d)/.exec(ts);
  return m ? (m[1] ?? ts) : ts;
}
