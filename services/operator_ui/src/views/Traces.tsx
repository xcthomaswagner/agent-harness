import { useMemo, useState } from "preact/hooks";
import { ViewHead } from "../chrome";
import { Button, Chip, Pill, Table } from "../primitives";
import { fetchHeaders } from "../api/key";
import type { PillTone } from "../primitives";
import { useFeed } from "../hooks/useFeed";
import type { TraceStatus, TraceSummary, TracesResponse } from "../api/types";
import { href, navigate } from "../router";
import { readableErrorText } from "./actionFeedback";
import type { ActionNotice } from "./actionFeedback";
import { traceCountsFromResponse } from "./format";

type StatusFilter = TraceStatus | "all";
type TraceLifecycleAction = "suppressed" | "misfire" | "stale" | "open";
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

export function TracesView() {
  const [filter, setFilter] = useState<StatusFilter>("all");
  const [offset, setOffset] = useState(0);
  const [includeHidden, setIncludeHidden] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [cleanupBusy, setCleanupBusy] = useState(false);
  const [notice, setNotice] = useState<ActionNotice | null>(null);
  const statusQuery = filter === "all" ? "" : `&status=${encodeURIComponent(filter)}`;
  const feed = useFeed<TracesResponse>(
    `/api/operator/traces?limit=${PAGE_SIZE}&offset=${offset}&include_hidden=${includeHidden ? "true" : "false"}${statusQuery}`,
    { clearOnUrlChange: true },
  );
  const countFeed = useFeed<TracesResponse>(
    `/api/operator/traces?limit=500&offset=0&include_hidden=${includeHidden ? "true" : "false"}`,
  );

  const counts = useMemo(() => {
    const source = countFeed.data ?? (filter === "all" ? feed.data : undefined);
    return traceCountsFromResponse(source);
  }, [countFeed.data, feed.data, filter]);

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
            onClick={() => {
              setFilter(f.value);
              setOffset(0);
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
          }}
        >
          {includeHidden ? "Hide hidden" : "Show hidden"}
        </Button>
        <Button
          size="sm"
          disabled={cleanupBusy}
          onClick={() => {
            void reconcileStaleRuns(feed.refresh, setCleanupBusy, setNotice);
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
      {notice && (
        <div class={`op-action-notice is-${notice.tone}`} role="status">
          {notice.text}
        </div>
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
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
            >
              Previous
            </Button>
            <Button
              size="sm"
              disabled={offset + PAGE_SIZE >= feed.data.count}
              onClick={() => setOffset(offset + PAGE_SIZE)}
            >
              Next
            </Button>
          </div>
        </div>
        <Table<TraceSummary>
          large
          rowKey={(t) => t.id}
          rows={feed.data.traces}
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
                        void markTrace(t.id, "open", feed.refresh, setBusyId, setNotice);
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
                          void markTrace(
                            t.id,
                            "suppressed",
                            feed.refresh,
                            setBusyId,
                            setNotice,
                          );
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
                          void markTrace(
                            t.id,
                            "misfire",
                            feed.refresh,
                            setBusyId,
                            setNotice,
                          );
                        }}
                      >
                        Misfire
                      </Button>
                      {t.lifecycle_state === "stale" && (
                        <Button
                          size="sm"
                          disabled={busyId === t.id}
                          onClick={(e) => {
                            e.stopPropagation();
                            void markTrace(
                              t.id,
                              "open",
                              feed.refresh,
                              setBusyId,
                              setNotice,
                            );
                          }}
                        >
                          Mark Active
                        </Button>
                      )}
                      {t.lifecycle_state !== "stale" && t.status !== "done" && (
                        <Button
                          size="sm"
                          disabled={busyId === t.id}
                          onClick={(e) => {
                            e.stopPropagation();
                            void markTrace(
                              t.id,
                              "stale",
                              feed.refresh,
                              setBusyId,
                              setNotice,
                            );
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
        </>
      )}
    </>
  );
}

async function markTrace(
  ticketId: string,
  state: TraceLifecycleAction,
  refresh: () => void,
  setBusyId: (id: string | null) => void,
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
  setBusyId(ticketId);
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
    setBusyId(null);
  }
}

async function reconcileStaleRuns(
  refresh: () => void,
  setCleanupBusy: (value: boolean) => void,
  setNotice?: (notice: ActionNotice) => void,
) {
  const raw = window.prompt("Stale after hours", "168") ?? "";
  const staleAfterHours = Number.parseInt(raw.trim(), 10);
  if (!Number.isFinite(staleAfterHours) || staleAfterHours <= 0) return;
  setCleanupBusy(true);
  setNotice?.({ tone: "warn", text: "Reconciling stale traces..." });
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
    if (!res.ok) {
      const text = await res.text();
      throw new Error(`${res.status}: ${readableErrorText(text)}`);
    }
    const data = await res.json();
    setNotice?.({
      tone: "ok",
      text: `Marked ${data.matched ?? 0} PR runs stale.`,
    });
    refresh();
  } catch (err) {
    const detail = err instanceof Error ? err.message : String(err);
    setNotice?.({ tone: "err", text: `Reconcile failed: ${detail}` });
  } finally {
    setCleanupBusy(false);
  }
}
