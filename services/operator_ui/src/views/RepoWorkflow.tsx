import { useEffect, useMemo, useState } from "preact/hooks";
import { ViewHead } from "../chrome";
import { readableErrorText } from "../api/errors";
import { fetchHeaders } from "../api/key";
import type {
  RepoWorkflowDraftResponse,
  RepoWorkflowEvidence,
  RepoWorkflowFinding,
  RepoWorkflowOptionsResponse,
  RepoWorkflowProfileOption,
  RepoWorkflowSaveResponse,
} from "../api/types";
import { useFeed } from "../hooks/useFeed";
import { Button, SectionHeader, Table } from "../primitives";

type ActionState = "idle" | "busy" | "ok" | "error";
const MANUAL_PROFILE_VALUE = "__manual__";

export function RepoWorkflowView() {
  const options = useFeed<RepoWorkflowOptionsResponse>("/api/operator/repo-workflow/options", {
    intervalMs: 0,
  });
  const profiles = options.data?.profiles ?? [];
  const [clientProfile, setClientProfile] = useState("");
  const [repoPath, setRepoPath] = useState("");
  const [draft, setDraft] = useState<RepoWorkflowDraftResponse | null>(null);
  const [editorText, setEditorText] = useState("");
  const [state, setState] = useState<ActionState>("idle");
  const [message, setMessage] = useState("");

  useEffect(() => {
    if (clientProfile || repoPath || profiles.length === 0) return;
    const firstReady = profiles.find((profile) => profile.repo_exists) ?? profiles[0];
    if (!firstReady) return;
    setClientProfile(firstReady.client_profile);
    setRepoPath(firstReady.repo_path);
  }, [clientProfile, profiles, repoPath]);

  const selectedProfile = useMemo(
    () => profiles.find((profile) => profile.client_profile === clientProfile) ?? null,
    [clientProfile, profiles],
  );
  const warningCount = (draft?.warnings.length ?? 0) + (draft?.validation.length ?? 0);

  const scan = async (preferExisting: boolean) => {
    setState("busy");
    setMessage(preferExisting ? "Validating existing workflow..." : "Generating draft...");
    try {
      const data = await postDraft(clientProfile, repoPath);
      setDraft(data);
      const nextText = preferExisting && data.existing_text ? data.existing_text : data.draft_text;
      setEditorText(nextText);
      setState("ok");
      setMessage(
        preferExisting && data.existing_text
          ? "Existing WORKFLOW.md loaded with validation notes."
          : preferExisting
            ? "No existing WORKFLOW.md found; generated a draft."
            : "Draft generated from repo evidence.",
      );
    } catch (err) {
      setState("error");
      setMessage(err instanceof Error ? err.message : String(err));
    }
  };

  const save = async () => {
    setState("busy");
    setMessage("Saving WORKFLOW.md...");
    try {
      const data = await putWorkflow(clientProfile, repoPath, editorText);
      const refreshed = await postDraft(clientProfile, repoPath);
      setState("ok");
      setMessage(`Saved ${data.bytes} bytes to ${data.workflow_path}.`);
      setDraft(refreshed);
      setEditorText(refreshed.existing_text || editorText);
    } catch (err) {
      setState("error");
      setMessage(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <>
      <ViewHead
        sup="Ops · repo workflow"
        title="Repo Workflow"
        sub="Generate and maintain repo-local WORKFLOW.md overlays."
        rnum={String(profiles.length)}
        rlabel="Profiles"
      />

      {options.status === "error" && (
        <div class="op-action-notice is-err">Profiles unavailable: {options.error}</div>
      )}
      {message && (
        <div
          class={`op-action-notice is-${
            state === "error" ? "err" : state === "ok" ? "ok" : "warn"
          }`}
          role="status"
        >
          {message}
        </div>
      )}

      <section class="op-section">
        <div class="op-workflow-target">
          <label class="op-workflow-field">
            <span>Client profile</span>
            <select
              value={clientProfile || MANUAL_PROFILE_VALUE}
              onChange={(event) => {
                const raw = (event.target as HTMLSelectElement).value;
                const next = raw === MANUAL_PROFILE_VALUE ? "" : raw;
                setClientProfile(next);
                const option = profiles.find((profile) => profile.client_profile === next);
                if (option) setRepoPath(option.repo_path);
              }}
            >
              <option value={MANUAL_PROFILE_VALUE}>Manual path</option>
              {profiles.map((profile) => (
                <option key={profile.client_profile} value={profile.client_profile}>
                  {profile.client_profile}
                </option>
              ))}
            </select>
          </label>
          <label class="op-workflow-field is-path">
            <span>Repository path</span>
            <input
              type="text"
              value={repoPath}
              placeholder="/path/to/client/repo"
              onInput={(event) => {
                const value = (event.target as HTMLInputElement).value;
                setRepoPath(value);
                const selected = profiles.find(
                  (profile) => profile.client_profile === clientProfile,
                );
                if (selected && selected.repo_path !== value) {
                  setClientProfile("");
                }
              }}
            />
          </label>
          <div class="op-workflow-actions">
            <Button
              variant="primary"
              disabled={state === "busy" || !repoPath.trim()}
              onClick={() => scan(false)}
            >
              Generate Draft
            </Button>
            <Button disabled={state === "busy" || !repoPath.trim()} onClick={() => scan(true)}>
              Validate Existing
            </Button>
            <Button
              variant="primary"
              disabled={state === "busy" || !editorText.trim()}
              onClick={save}
            >
              Save WORKFLOW.md
            </Button>
          </div>
        </div>
      </section>

      <WorkflowSummary
        draft={draft}
        selectedProfile={selectedProfile}
        warningCount={warningCount}
      />

      <section class="op-workflow-layout">
        <div class="op-workflow-left">
          <FindingPanel title="Warnings" items={draft?.warnings ?? []} />
          <FindingPanel title="Validation" items={draft?.validation ?? []} />
          <EvidencePanel evidence={draft?.evidence ?? []} />
        </div>
        <div class="op-workflow-editor-wrap">
          <SectionHeader
            label="WORKFLOW.md"
            right={draft?.workflow_exists ? "existing file" : "draft"}
          />
          <textarea
            class="op-workflow-editor"
            value={editorText}
            spellcheck={false}
            onInput={(event) => setEditorText((event.target as HTMLTextAreaElement).value)}
          />
        </div>
      </section>
    </>
  );
}

function WorkflowSummary({
  draft,
  selectedProfile,
  warningCount,
}: {
  draft: RepoWorkflowDraftResponse | null;
  selectedProfile: RepoWorkflowProfileOption | null;
  warningCount: number;
}) {
  const profile = draft?.client_profile || selectedProfile?.client_profile || "manual";
  const platform = draft?.platform_profile || selectedProfile?.platform_profile || "—";
  const validation = draft?.detected.validation_commands.length ?? 0;
  const workflow = draft?.workflow_exists
    ? "present"
    : selectedProfile?.workflow_exists
      ? "present"
      : "missing";
  return (
    <section class="op-workflow-summary">
      <SummaryCell label="Profile" value={profile} />
      <SummaryCell label="Platform" value={platform} />
      <SummaryCell label="Validation" value={String(validation)} />
      <SummaryCell label="Workflow" value={workflow} />
      <SummaryCell label="Notes" value={String(warningCount)} tone={warningCount ? "warn" : "ok"} />
    </section>
  );
}

function SummaryCell({
  label,
  value,
  tone = "cool",
}: {
  label: string;
  value: string;
  tone?: "cool" | "warn" | "ok";
}) {
  return (
    <div class={`op-workflow-summary-cell is-${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function FindingPanel({ title, items }: { title: string; items: RepoWorkflowFinding[] }) {
  return (
    <section class="op-workflow-panel">
      <SectionHeader label={title} right={`${items.length}`} />
      {items.length === 0 ? (
        <div class="op-rail-log-conn">No notes.</div>
      ) : (
        <div class="op-workflow-findings">
          {items.map((item) => (
            <div class={`op-readiness-warning is-${item.severity}`} key={item.id}>
              <div class="op-readiness-warning-head">
                <span>{item.area}</span>
                <span>{item.severity}</span>
              </div>
              <div>{item.message}</div>
              <div class="op-readiness-recommendation">{item.recommendation}</div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function EvidencePanel({ evidence }: { evidence: RepoWorkflowEvidence[] }) {
  return (
    <section class="op-workflow-panel">
      <SectionHeader label="Evidence" right={`${evidence.length}`} />
      <Table<RepoWorkflowEvidence>
        rows={evidence.slice(0, 80)}
        rowKey={(row) => `${row.area}-${row.source}-${row.message}-${row.value}`}
        empty="Generate a draft to inspect repo evidence."
        columns={[
          {
            key: "area",
            label: "Area",
            width: "96px",
            render: (row) => row.area,
          },
          {
            key: "source",
            label: "Source",
            width: "150px",
            render: (row) => <span class="op-mono">{row.source}</span>,
          },
          {
            key: "message",
            label: "Finding",
            render: (row) => row.message,
          },
          {
            key: "value",
            label: "Value",
            render: (row) => row.value || "—",
          },
        ]}
      />
    </section>
  );
}

async function postDraft(
  clientProfile: string,
  repoPath: string,
): Promise<RepoWorkflowDraftResponse> {
  const res = await fetch("/api/operator/repo-workflow/draft", {
    method: "POST",
    headers: fetchHeaders({
      Accept: "application/json",
      "Content-Type": "application/json",
    }),
    credentials: "same-origin",
    body: JSON.stringify({
      client_profile: clientProfile,
      repo_path: repoPath,
    }),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status}: ${readableErrorText(text)}`);
  }
  return (await res.json()) as RepoWorkflowDraftResponse;
}

async function putWorkflow(
  clientProfile: string,
  repoPath: string,
  content: string,
): Promise<RepoWorkflowSaveResponse> {
  const res = await fetch("/api/operator/repo-workflow", {
    method: "PUT",
    headers: fetchHeaders({
      Accept: "application/json",
      "Content-Type": "application/json",
    }),
    credentials: "same-origin",
    body: JSON.stringify({
      client_profile: clientProfile,
      repo_path: repoPath,
      content,
    }),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status}: ${readableErrorText(text)}`);
  }
  return (await res.json()) as RepoWorkflowSaveResponse;
}
