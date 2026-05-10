import { useEffect, useState } from "preact/hooks";
import { readableErrorText } from "../api/errors";
import { fetchHeaders } from "../api/key";
import type {
  AutomationEventSummary,
  AutomationJobSummary,
  AutomationProfileOption,
  AutomationRunResponse,
  AutomationsResponse,
  AutomationUpdateResponse,
} from "../api/types";
import { ViewHead } from "../chrome";
import { useFeed } from "../hooks/useFeed";
import { Button, Pill, SectionHeader } from "../primitives";
import "./views.css";

type ActionState = "idle" | "busy" | "ok" | "error";

interface DraftState {
  enabled: boolean;
  interval_seconds: number;
  scope: string;
  config: Record<string, unknown>;
}

export function AutomationsView() {
  const feed = useFeed<AutomationsResponse>("/api/operator/automations", {
    intervalMs: 10_000,
  });
  const enabled = feed.data?.jobs.filter((job) => job.enabled).length ?? 0;
  const failures =
    feed.data?.jobs.filter((job) => job.last_run?.status === "failed").length ?? 0;

  return (
    <>
      <ViewHead
        sup="Operate · automations"
        title="Automations"
        sub="Scheduled harness jobs for cleanup, reconciliation, and active-run monitoring."
        rnum={feed.status === "loading" && !feed.data ? "—" : String(enabled)}
        rlabel="Enabled"
      />

      {feed.status === "loading" && !feed.data && (
        <div class="op-loading">Loading automations…</div>
      )}
      {feed.status === "error" && !feed.data && (
        <div class="op-error">Failed to load automations: {feed.error}</div>
      )}
      {feed.data && (
        <>
          <section class="op-automation-summary">
            <SummaryCell label="Jobs" value={String(feed.data.jobs.length)} />
            <SummaryCell label="Enabled" value={String(enabled)} />
            <SummaryCell label="Failures" value={String(failures)} tone={failures ? "err" : "ok"} />
            <SummaryCell label="Events" value={String(feed.data.recent_events.length)} />
          </section>

          <section class="op-section">
            <SectionHeader
              label="Job controls"
              right={feed.status === "refreshing" ? "Refreshing" : "Live config"}
            />
            <div class="op-automation-grid">
              {feed.data.jobs.map((job) => (
                <AutomationCard
                  key={job.job_key}
                  job={job}
                  intervals={feed.data!.interval_options}
                  profiles={feed.data!.profiles}
                  onChanged={feed.refresh}
                />
              ))}
            </div>
          </section>

          <section class="op-section">
            <SectionHeader label="Recent automation events" right="Newest first" />
            <EventList events={feed.data.recent_events} />
          </section>
        </>
      )}
    </>
  );
}

function SummaryCell({
  label,
  value,
  tone = "cool",
}: {
  label: string;
  value: string;
  tone?: "ok" | "err" | "cool";
}) {
  return (
    <div class="op-automation-summary-cell">
      <span>{label}</span>
      <strong class={`is-${tone}`}>{value}</strong>
    </div>
  );
}

function AutomationCard({
  job,
  intervals,
  profiles,
  onChanged,
}: {
  job: AutomationJobSummary;
  intervals: readonly number[];
  profiles: readonly AutomationProfileOption[];
  onChanged: () => void;
}) {
  const [draft, setDraft] = useState<DraftState>(() => draftFromJob(job));
  const [state, setState] = useState<ActionState>("idle");
  const [message, setMessage] = useState("");

  useEffect(() => {
    setDraft(draftFromJob(job));
  }, [job.job_key, job.updated_at, job.last_run?.id]);

  useEffect(() => {
    setState("idle");
    setMessage("");
  }, [job.job_key]);

  const save = async () => {
    setState("busy");
    setMessage("Saving settings...");
    try {
      await putAutomation(job.job_key, draft);
      setState("ok");
      setMessage("Settings saved.");
      onChanged();
    } catch (err) {
      setState("error");
      setMessage(err instanceof Error ? err.message : String(err));
    }
  };

  const runNow = async () => {
    setState("busy");
    setMessage("Running job...");
    try {
      const result = await postRun(job.job_key);
      setState(result.run.status === "failed" ? "error" : "ok");
      setMessage(result.run.error || result.run.summary || "Run complete.");
      onChanged();
    } catch (err) {
      setState("error");
      setMessage(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <article class="op-automation-card">
      <div class="op-automation-card-head">
        <div>
          <h2>{job.label}</h2>
          <p>{job.description}</p>
        </div>
        <Pill tone={job.enabled ? "active" : "cool"}>
          {job.enabled ? "enabled" : "off"}
        </Pill>
      </div>

      <div class="op-automation-meta">
        <Meta label="Next" value={job.enabled ? formatWhen(job.next_run_at) : "Off"} />
        <Meta label="Last" value={job.last_run ? formatWhen(job.last_run.started_at) : "Never"} />
        <Meta
          label="Result"
          value={job.last_run?.status ?? "—"}
          tone={job.last_run?.status === "failed" ? "err" : "cool"}
        />
      </div>

      <div class="op-automation-controls">
        <label class="op-check">
          <input
            type="checkbox"
            checked={draft.enabled}
            onChange={(event) =>
              setDraft((current) => ({
                ...current,
                enabled: (event.target as HTMLInputElement).checked,
              }))
            }
          />
          Enabled
        </label>

        <label class="op-automation-field">
          <span>Interval</span>
          <select
            value={String(draft.interval_seconds)}
            onChange={(event) =>
              setDraft((current) => ({
                ...current,
                interval_seconds: Number((event.target as HTMLSelectElement).value),
              }))
            }
          >
            {intervals.map((seconds) => (
              <option value={String(seconds)} key={seconds}>
                {formatInterval(seconds)}
              </option>
            ))}
          </select>
        </label>

        <label class="op-automation-field">
          <span>Scope</span>
          <select
            value={draft.scope}
            onChange={(event) =>
              setDraft((current) => ({
                ...current,
                scope: (event.target as HTMLSelectElement).value,
              }))
            }
          >
            <option value="all">All profiles</option>
            {profiles.map((profile) => (
              <option value={profile.id} key={profile.id}>
                {profile.name || profile.id}
              </option>
            ))}
          </select>
        </label>

        <ConfigControls jobKey={job.job_key} draft={draft} setDraft={setDraft} />
      </div>

      <div class="op-setup-actions">
        <Button size="sm" onClick={save} disabled={state === "busy"}>
          Save settings
        </Button>
        <Button size="sm" variant="primary" onClick={runNow} disabled={state === "busy"}>
          Run now
        </Button>
      </div>

      {message && (
        <div class={`op-action-notice is-${state === "error" ? "err" : state === "ok" ? "ok" : "warn"}`}>
          {message}
        </div>
      )}
    </article>
  );
}

function ConfigControls({
  jobKey,
  draft,
  setDraft,
}: {
  jobKey: string;
  draft: DraftState;
  setDraft: (updater: (current: DraftState) => DraftState) => void;
}) {
  const updateNumber = (key: string, value: number) => {
    setDraft((current) => ({
      ...current,
      config: { ...current.config, [key]: value },
    }));
  };
  const updateBool = (key: string, value: boolean) => {
    setDraft((current) => ({
      ...current,
      config: { ...current.config, [key]: value },
    }));
  };

  if (jobKey === "trace_reconciliation") {
    return (
      <>
        <NumberField
          label="Stale after hours"
          value={numberConfig(draft.config, "stale_after_hours", 168)}
          onChange={(value) => updateNumber("stale_after_hours", value)}
        />
        <DryRunField
          checked={boolConfig(draft.config, "dry_run", false)}
          onChange={(value) => updateBool("dry_run", value)}
        />
      </>
    );
  }
  if (jobKey === "pipeline_watcher") {
    return (
      <>
        <NumberField
          label="Stale after minutes"
          value={numberConfig(draft.config, "stale_after_minutes", 120)}
          onChange={(value) => updateNumber("stale_after_minutes", value)}
        />
        <NumberField
          label="Event cooldown minutes"
          value={numberConfig(draft.config, "event_cooldown_minutes", 60)}
          onChange={(value) => updateNumber("event_cooldown_minutes", value)}
        />
        <DryRunField
          checked={boolConfig(draft.config, "dry_run", false)}
          onChange={(value) => updateBool("dry_run", value)}
        />
      </>
    );
  }
  if (jobKey === "stale_worktree_cleanup") {
    return (
      <>
        <NumberField
          label="Max age hours"
          value={numberConfig(draft.config, "max_age_hours", 48)}
          onChange={(value) => updateNumber("max_age_hours", value)}
        />
        <DryRunField
          checked={boolConfig(draft.config, "dry_run", true)}
          onChange={(value) => updateBool("dry_run", value)}
        />
      </>
    );
  }
  if (jobKey === "trace_archive_retention") {
    return (
      <>
        <NumberField
          label="Retention days"
          value={numberConfig(draft.config, "retention_days", 90)}
          onChange={(value) => updateNumber("retention_days", value)}
        />
        <DryRunField
          checked={boolConfig(draft.config, "dry_run", true)}
          onChange={(value) => updateBool("dry_run", value)}
        />
      </>
    );
  }
  return null;
}

function NumberField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: number;
  onChange: (value: number) => void;
}) {
  return (
    <label class="op-automation-field">
      <span>{label}</span>
      <input
        type="number"
        min="1"
        value={String(value)}
        onInput={(event) => onChange(Number((event.target as HTMLInputElement).value))}
      />
    </label>
  );
}

function DryRunField({
  checked,
  onChange,
}: {
  checked: boolean;
  onChange: (value: boolean) => void;
}) {
  return (
    <label class="op-check">
      <input
        type="checkbox"
        checked={checked}
        onChange={(event) => onChange((event.target as HTMLInputElement).checked)}
      />
      Dry run
    </label>
  );
}

function Meta({
  label,
  value,
  tone = "cool",
}: {
  label: string;
  value: string;
  tone?: "err" | "cool";
}) {
  return (
    <div class="op-automation-meta-cell">
      <span>{label}</span>
      <strong class={`is-${tone}`}>{value}</strong>
    </div>
  );
}

function EventList({ events }: { events: readonly AutomationEventSummary[] }) {
  if (events.length === 0) {
    return <div class="op-empty">No automation events yet.</div>;
  }
  return (
    <div class="op-automation-events">
      {events.map((event) => (
        <div class="op-automation-event" key={event.id}>
          <span>{formatWhen(event.created_at)}</span>
          <span class={`is-${event.severity}`}>{event.severity}</span>
          <span>{event.job_key}</span>
          <strong>{event.target_id || event.target_type || "system"}</strong>
          <p>{event.message}</p>
        </div>
      ))}
    </div>
  );
}

function draftFromJob(job: AutomationJobSummary): DraftState {
  return {
    enabled: job.enabled,
    interval_seconds: job.interval_seconds,
    scope: job.scope || "all",
    config: { ...job.config },
  };
}

function numberConfig(
  config: Record<string, unknown>,
  key: string,
  fallback: number,
): number {
  const value = config[key];
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function boolConfig(
  config: Record<string, unknown>,
  key: string,
  fallback: boolean,
): boolean {
  const value = config[key];
  return typeof value === "boolean" ? value : fallback;
}

function formatInterval(seconds: number): string {
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 24 * 3600) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86400)}d`;
}

function formatWhen(value: string): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

async function putAutomation(
  jobKey: string,
  draft: DraftState,
): Promise<AutomationUpdateResponse> {
  const res = await fetch(`/api/operator/automations/${encodeURIComponent(jobKey)}`, {
    method: "PUT",
    headers: fetchHeaders({
      Accept: "application/json",
      "Content-Type": "application/json",
    }),
    credentials: "same-origin",
    body: JSON.stringify(draft),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Save failed (${res.status}): ${readableErrorText(text)}`);
  }
  return (await res.json()) as AutomationUpdateResponse;
}

async function postRun(jobKey: string): Promise<AutomationRunResponse> {
  const res = await fetch(
    `/api/operator/automations/${encodeURIComponent(jobKey)}/run`,
    {
      method: "POST",
      headers: fetchHeaders({ Accept: "application/json" }),
      credentials: "same-origin",
    },
  );
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Run failed (${res.status}): ${readableErrorText(text)}`);
  }
  return (await res.json()) as AutomationRunResponse;
}
