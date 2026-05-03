import { useMemo } from "preact/hooks";
import { ViewHead } from "../chrome";
import { Chip, SectionHeader, Sparkline, Table } from "../primitives";
import { useFeed } from "../hooks/useFeed";
import type {
  AutonomyByTypeRow,
  AutonomyEscapedDefect,
  AutonomyResponse,
  AutonomyTrendPoint,
  ProfilesResponse,
} from "../api/types";
import { href, navigate } from "../router";
import { pct } from "./format";

interface Props {
  profile?: string;
}

export function AutonomyView({ profile }: Props) {
  // Profile switcher list — reuse the Home endpoint so we get the same
  // authoritative set of client profiles.
  const profiles = useFeed<ProfilesResponse>("/api/operator/profiles");
  const activeProfile = profile ?? profiles.data?.profiles[0]?.id;

  const feed = useFeed<AutonomyResponse>(
    activeProfile ? `/api/operator/autonomy/${encodeURIComponent(activeProfile)}` : null,
  );

  if (!activeProfile && profiles.status !== "loading") {
    return (
      <>
        <ViewHead
          sup="Ops · autonomy"
          title="Autonomy report"
          sub="No client profiles configured yet."
        />
        <div class="op-empty">
          Configure a client profile under runtime/client-profiles/
          to see an autonomy report.
        </div>
      </>
    );
  }

  return (
    <>
      <ViewHead
        sup={`Ops · autonomy${activeProfile ? ` · ${activeProfile}` : ""}`}
        title={
          activeProfile
            ? `Autonomy report — ${activeProfile}`
            : "Autonomy report"
        }
        sub="First-pass accept, escape, catch, and auto-merge over 30 days."
        right={
          profiles.data && (
            <div style={{ display: "flex", gap: "8px", flexWrap: "wrap" }}>
              {profiles.data.profiles.map((p) => (
                <Chip
                  key={p.id}
                  label={p.id}
                  on={p.id === activeProfile}
                  onClick={() =>
                    navigate(`/autonomy/${encodeURIComponent(p.id)}`)
                  }
                />
              ))}
            </div>
          )
        }
      />

      {feed.status === "loading" && !feed.data && (
        <div class="op-loading">Loading metrics…</div>
      )}
      {feed.status === "error" && !feed.data && (
        <div class="op-error">Failed to load: {feed.error}</div>
      )}
      {feed.data && <AutonomyBody data={feed.data} />}
    </>
  );
}

function AutonomyBody({ data }: { data: AutonomyResponse }) {
  return (
    <>
      <section class="op-metric-band">
        <MetricCell
          label="First-pass"
          value={pct(data.metrics.fpa)}
          sub={`${data.metrics.sample_size} runs`}
        />
        <MetricCell
          label="Escape rate"
          value={pct(data.metrics.escape)}
          sub="30d escaped defects"
        />
        <MetricCell
          label="Catch rate"
          value={pct(data.metrics.catch)}
          sub="self-review coverage"
        />
        <MetricCell
          label="Auto-merge"
          value={pct(data.metrics.auto_merge)}
          sub={`${data.metrics.merged_count} merged`}
        />
      </section>

      <section class="op-section">
        <SectionHeader label="Trends" right="30-day daily" />
        <div class="op-trend-grid">
          <TrendCard
            label="First-pass"
            points={data.trends.fpa}
            color="var(--signal-ok)"
          />
          <TrendCard
            label="Escape"
            points={data.trends.escape}
            color="var(--signal-err)"
          />
        </div>
        <div class="op-trend-card">
          <span class="op-trend-card-lbl">Auto-merge adoption</span>
          <span
            class="op-trend-card-val"
            style={{ color: "var(--signal-active)" }}
          >
            <Sparkline
              values={data.trends.auto_merge.map((p) => p.value)}
              width={640}
              height={60}
            />
          </span>
        </div>
      </section>

      <section class="op-section">
        <SectionHeader label="By ticket type" right={`${data.by_type.length} types`} />
        <ByTypeTable rows={data.by_type} />
      </section>

      <section class="op-section">
        <SectionHeader label="Escaped defects" right="last 30 days" />
        <EscapedDefectsTable rows={data.escaped} />
      </section>
    </>
  );
}

function MetricCell({
  label,
  value,
  sub,
}: {
  label: string;
  value: string;
  sub: string;
}) {
  return (
    <div class="op-metric-cell">
      <span class="op-metric-label">{label}</span>
      <span class="op-metric-val">{value}</span>
      <span class="op-metric-sub">{sub}</span>
    </div>
  );
}

function TrendCard({
  label,
  points,
  color,
}: {
  label: string;
  points: AutonomyTrendPoint[];
  color: string;
}) {
  const latest = useMemo(() => {
    // Walk backwards looking for the first non-null value to surface as
    // the headline numeric.
    for (let i = points.length - 1; i >= 0; i--) {
      const value = points[i]?.value;
      if (value !== null && value !== undefined) return value;
    }
    return null;
  }, [points]);
  return (
    <div class="op-trend-card">
      <span class="op-trend-card-lbl">{label}</span>
      <span class="op-trend-card-val" style={{ color }}>
        {pct(latest)}
      </span>
      <span style={{ color }}>
        <Sparkline values={points.map((p) => p.value)} width={320} height={44} />
      </span>
    </div>
  );
}

function ByTypeTable({ rows }: { rows: AutonomyByTypeRow[] }) {
  return (
    <Table<AutonomyByTypeRow>
      rowKey={(r) => r.ticket_type}
      rows={rows}
      empty="No by-type data in this window."
      columns={[
        {
          key: "type",
          label: "Type",
          render: (r) => <span class="op-mono">{r.ticket_type}</span>,
        },
        {
          key: "volume",
          label: "Volume",
          numeric: true,
          render: (r) => r.volume,
        },
        {
          key: "fpa",
          label: "First-pass",
          numeric: true,
          render: (r) => pct(r.fpa),
        },
        {
          key: "escape",
          label: "Escape",
          numeric: true,
          render: (r) => (
            <span
              class={r.escape !== null && r.escape > 0.2 ? "op-escape-severe" : undefined}
            >
              {pct(r.escape)}
            </span>
          ),
        },
        {
          key: "catch",
          label: "Catch",
          numeric: true,
          render: (r) => pct(r.catch),
        },
      ]}
    />
  );
}

function EscapedDefectsTable({
  rows,
}: {
  rows: AutonomyEscapedDefect[];
}) {
  return (
    <Table<AutonomyEscapedDefect>
      rowKey={(r) => r.id}
      rows={rows}
      empty="No escaped defects in this window."
      columns={[
        {
          key: "id",
          label: "Defect",
          width: "120px",
          render: (r) => <span class="op-mono">{r.id}</span>,
        },
        {
          key: "ticket",
          label: "Ticket",
          width: "140px",
          render: (r) =>
            r.ticket_id ? (
              <a href={href({ name: "trace-detail", id: r.ticket_id })}>
                <span class="op-mono">{r.ticket_id}</span>
              </a>
            ) : (
              "—"
            ),
        },
        {
          key: "severity",
          label: "Severity",
          width: "90px",
          render: (r) => <span class="op-mono">{r.severity}</span>,
        },
        {
          key: "where",
          label: "Where",
          render: (r) => <span class="op-mono">{r.where || "—"}</span>,
        },
        {
          key: "note",
          label: "Note",
          render: (r) => r.note || "—",
        },
      ]}
    />
  );
}
