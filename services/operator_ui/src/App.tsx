import { useState } from "preact/hooks";
import {
  BrandGlyph,
  Button,
  Chip,
  KPITile,
  PhaseDots,
  Pill,
  SearchInput,
  SectionHeader,
  Sparkline,
  Table,
  Wordmark,
} from "./primitives";
import type { PhaseState } from "./primitives";

/**
 * Placeholder App — renders the primitives showcase so designers and
 * reviewers can eyeball every component in isolation at ``/operator``
 * before views land. Commit 4 replaces this with the real chrome +
 * router.
 */
export function App() {
  const [search, setSearch] = useState("");
  const [activeChip, setActiveChip] = useState("all");

  const demoTable = [
    { id: "HARN-2043", title: "Rollback-safe deploy", status: "active" as const },
    { id: "HARN-2041", title: "Renewal pricing mismatch", status: "active" as const },
    { id: "HARN-2039", title: "PDP hero RTL layout", status: "warn" as const },
    { id: "HARN-2037", title: "VPC peering tag propagation", status: "cool" as const },
  ];

  const phases: readonly PhaseState[] = ["done", "done", "active", "pending", "pending"];
  const failPhases: readonly PhaseState[] = ["done", "done", "fail", "pending", "pending"];

  return (
    <main style={{ padding: "40px 48px", maxWidth: "1280px" }}>
      <header style={{ display: "flex", alignItems: "center", gap: "12px", marginBottom: "48px" }}>
        <BrandGlyph />
        <Wordmark />
        <span style={{ marginLeft: "auto", fontFamily: "var(--font-mono)", fontSize: "10px", color: "var(--ink-500)", textTransform: "uppercase", letterSpacing: "0.12em" }}>
          Primitives preview · commit 3
        </span>
      </header>

      <h1 style={{ fontFamily: "var(--font-serif)", fontSize: "38px", margin: "0 0 8px", letterSpacing: "-0.01em" }}>
        Design system
      </h1>
      <p style={{ color: "var(--ink-600)", margin: "0 0 40px" }}>
        Low-level primitives. Composed into the real views in later commits.
      </p>

      <section style={{ marginBottom: "48px" }}>
        <SectionHeader label="Status pills" right="5 tones" />
        <div style={{ display: "flex", gap: "10px", flexWrap: "wrap" }}>
          <Pill tone="active">In-flight</Pill>
          <Pill tone="ok">Merged</Pill>
          <Pill tone="warn">Needs clarify</Pill>
          <Pill tone="err">Failed</Pill>
          <Pill tone="cool">Queued</Pill>
        </div>
      </section>

      <section style={{ marginBottom: "48px" }}>
        <SectionHeader label="Phase dots" right="5-phase pipeline" />
        <div style={{ display: "flex", gap: "32px", alignItems: "center" }}>
          <PhaseDots phases={phases} />
          <PhaseDots phases={failPhases} />
          <PhaseDots phases={["done", "done", "done", "done", "done"]} />
        </div>
      </section>

      <section style={{ marginBottom: "48px" }}>
        <SectionHeader label="Chips" right="filter row" />
        <div style={{ display: "flex", gap: "8px", flexWrap: "wrap" }}>
          {(["all", "in-flight", "stuck", "queued", "done"] as const).map((id) => (
            <Chip
              key={id}
              label={id}
              count={id === "all" ? 24 : id === "in-flight" ? 3 : id === "stuck" ? 1 : id === "queued" ? 2 : 18}
              on={activeChip === id}
              onClick={() => setActiveChip(id)}
            />
          ))}
        </div>
      </section>

      <section style={{ marginBottom: "48px" }}>
        <SectionHeader label="KPI tiles" right="metric band" />
        <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", border: "var(--rule)" }}>
          <div style={{ borderRight: "var(--rule)" }}>
            <KPITile
              label="First-pass accept"
              value="81"
              suffix="%"
              sub="24h window"
              trend={[0.62, 0.65, 0.71, 0.69, 0.74, 0.78, 0.81]}
              trendColor="var(--signal-ok)"
            />
          </div>
          <div style={{ borderRight: "var(--rule)" }}>
            <KPITile
              label="Escape rate"
              value="7"
              suffix="%"
              sub="30d rolling"
              trend={[0.11, 0.10, 0.09, 0.09, 0.08, 0.08, 0.07]}
              trendColor="var(--signal-err)"
            />
          </div>
          <div style={{ borderRight: "var(--rule)" }}>
            <KPITile
              label="Auto-merge"
              value="62"
              suffix="%"
              sub="of eligible"
              trend={[0.41, 0.48, 0.52, 0.55, 0.58, 0.60, 0.62]}
              trendColor="var(--signal-active)"
            />
          </div>
          <KPITile
            label="Median cycle"
            value="11"
            suffix="m"
            sub="analyst → PR"
          />
        </div>
      </section>

      <section style={{ marginBottom: "48px" }}>
        <SectionHeader label="Sparkline" />
        <div style={{ display: "flex", gap: "32px" }}>
          <span style={{ color: "var(--signal-ok)" }}>
            <Sparkline values={[0.5, 0.6, 0.58, 0.7, 0.72, 0.8, 0.81]} width={200} height={40} />
          </span>
          <span style={{ color: "var(--signal-err)" }}>
            <Sparkline values={[0.12, 0.11, null, 0.09, 0.08, 0.07]} width={200} height={40} />
          </span>
        </div>
      </section>

      <section style={{ marginBottom: "48px" }}>
        <SectionHeader label="Buttons" right="4 variants × 2 sizes" />
        <div style={{ display: "flex", gap: "10px", flexWrap: "wrap" }}>
          <Button>Open worktree</Button>
          <Button variant="primary">Merge</Button>
          <Button variant="ghost">Cancel</Button>
          <Button variant="danger">Escalate</Button>
          <Button size="sm">Edit</Button>
          <Button size="sm" variant="primary">
            Apply now
          </Button>
        </div>
      </section>

      <section style={{ marginBottom: "48px" }}>
        <SectionHeader label="Search input" right="mono hotkey" />
        <SearchInput
          value={search}
          onInput={setSearch}
          placeholder="Filter tickets…"
          hotkey="/"
          focusOnHotkey
        />
      </section>

      <section style={{ marginBottom: "48px" }}>
        <SectionHeader label="Table" right={`${demoTable.length} rows`} />
        <Table
          rowKey={(r) => r.id}
          columns={[
            { key: "id", label: "Ticket", width: "120px", render: (r) => <span class="mono">{r.id}</span> },
            { key: "title", label: "Title", render: (r) => r.title },
            { key: "status", label: "Status", width: "140px", render: (r) => <Pill tone={r.status}>{r.status}</Pill> },
          ]}
          rows={demoTable}
          isLive={(r) => r.status === "active"}
        />
      </section>
    </main>
  );
}
