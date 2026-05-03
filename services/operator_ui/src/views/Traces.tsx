import { useMemo, useState } from "preact/hooks";
import { ViewHead } from "../chrome";
import { Chip, Pill, Table } from "../primitives";
import type { PillTone } from "../primitives";
import { useFeed } from "../hooks/useFeed";
import type { TraceStatus, TraceSummary, TracesResponse } from "../api/types";
import { href, navigate } from "../router";

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

export function TracesView() {
  const [filter, setFilter] = useState<StatusFilter>("all");
  const feed = useFeed<TracesResponse>("/api/operator/traces?limit=200");

  const filtered = useMemo(() => {
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
    if (!feed.data) return base;
    base.all = feed.data.traces.length;
    for (const t of feed.data.traces) {
      base[t.status] += 1;
    }
    return base;
  }, [feed.data]);

  return (
    <>
      <ViewHead
        sup="Pipeline · traces"
        title="Traces"
        sub="Every run across every profile. Newest first."
        rnum={String(counts.all)}
        rlabel="Runs · total"
      />

      <div
        style={{
          display: "flex",
          gap: "8px",
          marginBottom: "24px",
          flexWrap: "wrap",
        }}
      >
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

      {feed.status === "loading" && (
        <div class="op-loading">Loading traces…</div>
      )}
      {feed.status === "error" && (
        <div class="op-error">Failed to load traces: {feed.error}</div>
      )}
      {feed.data && (
        <Table<TraceSummary>
          large
          rowKey={(t) => t.id}
          rows={filtered}
          isLive={(t) => t.status === "in-flight"}
          onRowClick={(t) => navigate(`/traces/${encodeURIComponent(t.id)}`)}
          empty={
            filter === "all"
              ? "No runs recorded yet."
              : `No ${filter} traces right now.`
          }
          columns={[
            {
              key: "id",
              label: "Ticket",
              width: "140px",
              render: (t) => <span class="op-mono">{t.id}</span>,
            },
            {
              key: "title",
              label: "Title",
              render: (t) => (
                <a href={href({ name: "trace-detail", id: t.id })}>
                  {t.title || <em style={{ color: "var(--ink-500)" }}>(no title)</em>}
                </a>
              ),
            },
            {
              key: "status",
              label: "Status",
              width: "140px",
              render: (t) => (
                <Pill tone={STATUS_TONE[t.status]}>{t.raw_status}</Pill>
              ),
            },
            {
              key: "phase",
              label: "Phase",
              width: "140px",
              render: (t) => <span class="op-mono">{t.phase || "—"}</span>,
            },
            {
              key: "elapsed",
              label: "Elapsed",
              width: "100px",
              numeric: true,
              render: (t) => t.elapsed || "—",
            },
            {
              key: "pr",
              label: "PR",
              width: "80px",
              render: (t) =>
                t.pr_url ? (
                  <a href={t.pr_url} target="_blank" rel="noopener noreferrer" class="op-mono">
                    ↗
                  </a>
                ) : (
                  <span class="op-mono" style={{ color: "var(--ink-500)" }}>—</span>
                ),
            },
          ]}
        />
      )}
    </>
  );
}
