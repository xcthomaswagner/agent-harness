import { useMemo, useState } from "preact/hooks";
import { ViewHead } from "../chrome";
import { Button, Chip, Pill, Table } from "../primitives";
import { fetchHeaders } from "../api/key";
import type { PillTone } from "../primitives";
import { useFeed } from "../hooks/useFeed";
import type { TraceStatus, TraceSummary, TracesResponse } from "../api/types";
import { href, navigate } from "../router";

type StatusFilter = TraceStatus | "all";
type TraceLifecycleAction = "suppressed" | "misfire" | "stale" | "open";

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

export function TracesView() {
  const [filter, setFilter] = useState<StatusFilter>("all");
  const [includeHidden, setIncludeHidden] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [cleanupBusy, setCleanupBusy] = useState(false);
  const feed = useFeed<TracesResponse>(
    `/api/operator/traces?limit=200&include_hidden=${includeHidden ? "true" : "false"}`,
  );

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
      hidden: 0,
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
          f.value === "hidden" && !includeHidden ? null : (
          <Chip
            key={f.value}
            label={f.label}
            count={counts[f.value]}
            on={filter === f.value}
            onClick={() => setFilter(f.value)}
          />
          )
        ))}
        <Button
          size="sm"
          variant={includeHidden ? "danger" : "default"}
          onClick={() => {
            setIncludeHidden((v) => !v);
            if (filter === "hidden") setFilter("all");
          }}
        >
          {includeHidden ? "Hide hidden" : "Show hidden"}
        </Button>
        <Button
          size="sm"
          disabled={cleanupBusy}
          onClick={() => {
            void reconcileStaleRuns(feed.refresh, setCleanupBusy);
          }}
        >
          Reconcile stale
        </Button>
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
                <span title={t.state_reason || t.lifecycle_state || undefined}>
                  <Pill tone={STATUS_TONE[t.status]}>{t.raw_status}</Pill>
                </span>
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
              sortValue: (t) => Boolean(t.pr_url),
              render: (t) =>
                t.pr_url ? (
                  <a href={t.pr_url} target="_blank" rel="noopener noreferrer" class="op-mono">
                    ↗
                  </a>
                ) : (
                  <span class="op-mono" style={{ color: "var(--ink-500)" }}>—</span>
                ),
            },
            {
              key: "actions",
              label: "Actions",
              width: "248px",
              sortValue: (t) => t.lifecycle_state || t.status,
              render: (t) => (
                <span style={{ display: "flex", gap: "6px", justifyContent: "flex-end" }}>
                  {t.hidden ? (
                    <Button
                      size="sm"
                      disabled={busyId === t.id}
                      onClick={(e) => {
                        e.stopPropagation();
                        void markTrace(t.id, "open", feed.refresh, setBusyId);
                      }}
                    >
                      Restore
                    </Button>
                  ) : (
                    <>
                      <Button
                        size="sm"
                        disabled={busyId === t.id}
                        onClick={(e) => {
                          e.stopPropagation();
                          void markTrace(t.id, "suppressed", feed.refresh, setBusyId);
                        }}
                      >
                        Hide
                      </Button>
                      <Button
                        size="sm"
                        variant="danger"
                        disabled={busyId === t.id}
                        onClick={(e) => {
                          e.stopPropagation();
                          void markTrace(t.id, "misfire", feed.refresh, setBusyId);
                        }}
                      >
                        Misfire
                      </Button>
                      {t.lifecycle_state !== "stale" && t.status !== "done" && (
                        <Button
                          size="sm"
                          disabled={busyId === t.id}
                          onClick={(e) => {
                            e.stopPropagation();
                            void markTrace(t.id, "stale", feed.refresh, setBusyId);
                          }}
                        >
                          Stale
                        </Button>
                      )}
                    </>
                  )}
                </span>
              ),
            },
          ]}
        />
      )}
    </>
  );
}

async function markTrace(
  ticketId: string,
  state: TraceLifecycleAction,
  refresh: () => void,
  setBusyId: (id: string | null) => void,
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
  setBusyId(ticketId);
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
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    refresh();
  } finally {
    setBusyId(null);
  }
}

async function reconcileStaleRuns(
  refresh: () => void,
  setCleanupBusy: (value: boolean) => void,
) {
  const raw = window.prompt("Stale after hours", "168") ?? "";
  const staleAfterHours = Number.parseInt(raw.trim(), 10);
  if (!Number.isFinite(staleAfterHours) || staleAfterHours <= 0) return;
  setCleanupBusy(true);
  try {
    const res = await fetch("/api/operator/dashboard/reconcile-stale", {
      method: "POST",
      headers: fetchHeaders({
        Accept: "application/json",
        "Content-Type": "application/json",
      }),
      credentials: "same-origin",
      body: JSON.stringify({
        stale_after_hours: staleAfterHours,
        dry_run: false,
      }),
    });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    const data = await res.json();
    window.alert(`Marked ${data.matched ?? 0} PR runs stale.`);
    refresh();
  } finally {
    setCleanupBusy(false);
  }
}
