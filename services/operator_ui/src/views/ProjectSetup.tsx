import type { ComponentChildren } from "preact";
import { useEffect, useMemo, useState } from "preact/hooks";
import { readableErrorText } from "../api/errors";
import { fetchHeaders } from "../api/key";
import type {
  ProjectSetupInspectResponse,
  ProjectSetupNote,
  ProjectSetupOptionsResponse,
  ProjectSetupSaveResponse,
} from "../api/types";
import { ViewHead } from "../chrome";
import { useFeed } from "../hooks/useFeed";
import { Button, Pill, SectionHeader } from "../primitives";

type ActionState = "idle" | "busy" | "ok" | "error";
type LocalEnvField = {
  key: string;
  label: string;
  secret?: boolean;
  placeholder?: string;
};

type FormState = {
  profile_id: string;
  client_name: string;
  project_path: string;
  platform_profile: string;
  ticket_source_type: string;
  ticket_instance: string;
  project_key: string;
  ado_project_name: string;
  ai_label: string;
  quick_label: string;
  clarification_status: string;
  in_progress_status: string;
  done_status: string;
  source_control_type: string;
  github_repo: string;
  repo_url: string;
  source_org: string;
  repo_name: string;
  ado_org: string;
  ado_project: string;
  ado_repository_id: string;
  default_branch: string;
  branch_prefix: string;
  pr_reviewers: string;
  test_command: string;
  lint_command: string;
  build_command: string;
  e2e_command: string;
  unit_test_framework: string;
  integration_test_framework: string;
  e2e_test_framework: string;
  auto_merge_enabled: boolean;
  low_risk_ticket_types: string;
  create_directory: boolean;
  init_git: boolean;
  create_github_repo: boolean;
};

const EMPTY_FORM: FormState = {
  profile_id: "",
  client_name: "",
  project_path: "",
  platform_profile: "generic",
  ticket_source_type: "jira",
  ticket_instance: "",
  project_key: "",
  ado_project_name: "",
  ai_label: "ai-implement",
  quick_label: "ai-quick",
  clarification_status: "Needs Info",
  in_progress_status: "",
  done_status: "Done",
  source_control_type: "github",
  github_repo: "",
  repo_url: "",
  source_org: "",
  repo_name: "",
  ado_org: "",
  ado_project: "",
  ado_repository_id: "",
  default_branch: "main",
  branch_prefix: "ai/",
  pr_reviewers: "",
  test_command: "",
  lint_command: "",
  build_command: "",
  e2e_command: "",
  unit_test_framework: "",
  integration_test_framework: "",
  e2e_test_framework: "",
  auto_merge_enabled: false,
  low_risk_ticket_types: "bug, chore, config, dependency, docs",
  create_directory: false,
  init_git: false,
  create_github_repo: false,
};

export function ProjectSetupView() {
  const options = useFeed<ProjectSetupOptionsResponse>("/api/operator/project-setup/options", {
    intervalMs: 0,
  });
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [platformSettings, setPlatformSettings] = useState<Record<string, string>>({});
  const [profilePlatformSettings, setProfilePlatformSettings] = useState<Record<string, string>>({});
  const [inspect, setInspect] = useState<ProjectSetupInspectResponse | null>(null);
  const [saveResult, setSaveResult] = useState<ProjectSetupSaveResponse | null>(null);
  const [state, setState] = useState<ActionState>("idle");
  const [message, setMessage] = useState("");

  const selectedEnvSettings = useMemo(() => {
    const platform = form.platform_profile || "generic";
    return options.data?.platform_settings[platform] ?? [];
  }, [form.platform_profile, options.data]);
  const selectedProfileFields = useMemo(() => {
    const platform = form.platform_profile || "generic";
    return options.data?.profile_platform_fields[platform] ?? [];
  }, [form.platform_profile, options.data]);

  useEffect(() => {
    if (!inspect) return;
    setForm((current) => ({
      ...current,
      project_path: inspect.path || current.project_path,
      profile_id: current.profile_id || inspect.suggested_profile_id,
      client_name: current.client_name || inspect.suggested_client_name,
      platform_profile:
        current.platform_profile === "generic" && inspect.detected_platform
          ? inspect.detected_platform
          : current.platform_profile,
      github_repo: current.github_repo || inspect.github_repo,
      default_branch: current.default_branch || inspect.git_branch || "main",
      test_command:
        current.test_command || inspect.detected.validation_commands?.[0] || "",
      build_command:
        current.build_command ||
        inspect.detected.validation_commands?.find((cmd) => cmd.includes("build")) ||
        "",
    }));
  }, [inspect]);

  const patch = <K extends keyof FormState>(key: K, value: FormState[K]) => {
    setForm((current) => ({ ...current, [key]: value }));
    setSaveResult(null);
  };

  const inspectPath = async () => {
    setState("busy");
    setMessage("Inspecting local directory...");
    setSaveResult(null);
    try {
      const data = await postInspect(form.project_path);
      setInspect(data);
      setState("ok");
      setMessage(data.exists ? "Directory inspected." : "Directory does not exist yet.");
    } catch (err) {
      setState("error");
      setMessage(err instanceof Error ? err.message : String(err));
    }
  };

  const save = async () => {
    setState("busy");
    setMessage("Saving project setup...");
    try {
      const result = await putSetup(form, platformSettings, profilePlatformSettings);
      setSaveResult(result);
      setState("ok");
      setMessage(`Saved ${result.profile_id}. The harness will use ${result.project_path}.`);
    } catch (err) {
      setState("error");
      setMessage(err instanceof Error ? err.message : String(err));
    }
  };

  const sourceIsGithub = form.source_control_type === "github";
  const sourceIsAzure = form.source_control_type === "azure-repos";
  const ticketIsAdo = form.ticket_source_type === "ado";
  const credentialFields = credentialEnvFields({
    sourceIsGithub,
    sourceIsAzure,
    ticketIsAdo,
  });

  return (
    <>
      <ViewHead
        sup="Setup · project"
        title="Project Setup"
        sub="Point the harness at a local project, select a supported project type, and save the settings it needs to run."
        rnum={String(options.data?.profiles.length ?? 0)}
        rlabel="Profiles"
      />

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

      <section class="op-project-grid">
        <div class="op-project-main">
          <SetupPanel title="Local project" right="required">
            <div class="op-setup-fields">
              <Field label="Project directory" wide>
                <input
                  value={form.project_path}
                  placeholder="/Users/name/Projects/client-repo"
                  onInput={(event) =>
                    patch("project_path", (event.target as HTMLInputElement).value)
                  }
                />
              </Field>
              <div class="op-setup-actions">
                <Button
                  variant="primary"
                  disabled={state === "busy" || !form.project_path.trim()}
                  onClick={inspectPath}
                >
                  Inspect directory
                </Button>
                <label class="op-check">
                  <input
                    type="checkbox"
                    checked={form.create_directory}
                    onChange={(event) =>
                      patch("create_directory", (event.target as HTMLInputElement).checked)
                    }
                  />
                  Create if missing
                </label>
                <label class="op-check">
                  <input
                    type="checkbox"
                    checked={form.init_git}
                    onChange={(event) =>
                      patch("init_git", (event.target as HTMLInputElement).checked)
                    }
                  />
                  Initialize git
                </label>
              </div>
            </div>
          </SetupPanel>

          <SetupPanel title="Harness profile" right="routing">
            <div class="op-setup-fields">
              <Field label="Profile ID">
                <input
                  value={form.profile_id}
                  placeholder="client-project"
                  onInput={(event) =>
                    patch("profile_id", (event.target as HTMLInputElement).value)
                  }
                />
              </Field>
              <Field label="Client name">
                <input
                  value={form.client_name}
                  placeholder="Client Project"
                  onInput={(event) =>
                    patch("client_name", (event.target as HTMLInputElement).value)
                  }
                />
              </Field>
              <Field label="Project type">
                <select
                  value={form.platform_profile}
                  onChange={(event) => {
                    patch(
                      "platform_profile",
                      (event.target as HTMLSelectElement).value,
                    );
                    setPlatformSettings({});
                    setProfilePlatformSettings({});
                  }}
                >
                  {(options.data?.platforms ?? ["generic"]).map((platform) => (
                    <option key={platform} value={platform}>
                      {label(platform)}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Low-risk types">
                <input
                  value={form.low_risk_ticket_types}
                  onInput={(event) =>
                    patch("low_risk_ticket_types", (event.target as HTMLInputElement).value)
                  }
                />
              </Field>
              <label class="op-check">
                <input
                  type="checkbox"
                  checked={form.auto_merge_enabled}
                  onChange={(event) =>
                    patch("auto_merge_enabled", (event.target as HTMLInputElement).checked)
                  }
                />
                Enable auto-merge for eligible low-risk tickets
              </label>
            </div>
          </SetupPanel>

          <SetupPanel title="Ticket source" right="trigger">
            <div class="op-setup-fields">
              <Field label="Ticket system">
                <select
                  value={form.ticket_source_type}
                  onChange={(event) =>
                    patch("ticket_source_type", (event.target as HTMLSelectElement).value)
                  }
                >
                  {(options.data?.ticket_sources ?? ["jira", "ado"]).map((source) => (
                    <option key={source} value={source}>
                      {source.toUpperCase()}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label={ticketIsAdo ? "ADO org URL" : "Jira base URL"}>
                <input
                  value={form.ticket_instance}
                  placeholder={ticketIsAdo ? "https://dev.azure.com/org" : "https://client.atlassian.net"}
                  onInput={(event) =>
                    patch("ticket_instance", (event.target as HTMLInputElement).value)
                  }
                />
              </Field>
              <Field label="Ticket prefix">
                <input
                  value={form.project_key}
                  placeholder="PROJ"
                  onInput={(event) =>
                    patch("project_key", (event.target as HTMLInputElement).value)
                  }
                />
              </Field>
              {ticketIsAdo && (
                <Field label="ADO project name">
                  <input
                    value={form.ado_project_name}
                    placeholder="Research and Development"
                    onInput={(event) =>
                      patch("ado_project_name", (event.target as HTMLInputElement).value)
                    }
                  />
                </Field>
              )}
              <Field label="Trigger label">
                <input
                  value={form.ai_label}
                  onInput={(event) =>
                    patch("ai_label", (event.target as HTMLInputElement).value)
                  }
                />
              </Field>
              <Field label="Quick label">
                <input
                  value={form.quick_label}
                  onInput={(event) =>
                    patch("quick_label", (event.target as HTMLInputElement).value)
                  }
                />
              </Field>
            </div>
          </SetupPanel>

          <SetupPanel title="Source control" right={sourceIsGithub ? "GitHub" : "Azure Repos"}>
            <div class="op-setup-fields">
              <Field label="Source control">
                <select
                  value={form.source_control_type}
                  onChange={(event) =>
                    patch("source_control_type", (event.target as HTMLSelectElement).value)
                  }
                >
                  {(options.data?.source_controls ?? ["github", "azure-repos"]).map((source) => (
                    <option key={source} value={source}>
                      {source === "azure-repos" ? "Azure Repos" : "GitHub"}
                    </option>
                  ))}
                </select>
              </Field>
              {sourceIsGithub ? (
                <>
                  <Field label="GitHub repo">
                    <input
                      value={form.github_repo}
                      placeholder="owner/repo"
                      onInput={(event) =>
                        patch("github_repo", (event.target as HTMLInputElement).value)
                      }
                    />
                  </Field>
                  <label class="op-check">
                    <input
                      type="checkbox"
                      checked={form.create_github_repo}
                      onChange={(event) =>
                        patch(
                          "create_github_repo",
                          (event.target as HTMLInputElement).checked,
                        )
                      }
                    />
                    Create missing GitHub repo with gh
                  </label>
                </>
              ) : (
                <>
                  <Field label="ADO org URL">
                    <input
                      value={form.ado_org}
                      placeholder="https://dev.azure.com/org"
                      onInput={(event) =>
                        patch("ado_org", (event.target as HTMLInputElement).value)
                      }
                    />
                  </Field>
                  <Field label="ADO project">
                    <input
                      value={form.ado_project}
                      onInput={(event) =>
                        patch("ado_project", (event.target as HTMLInputElement).value)
                      }
                    />
                  </Field>
                  <Field label="Repository ID">
                    <input
                      value={form.ado_repository_id}
                      onInput={(event) =>
                        patch(
                          "ado_repository_id",
                          (event.target as HTMLInputElement).value,
                        )
                      }
                    />
                  </Field>
                </>
              )}
              <Field label="Default branch">
                <input
                  value={form.default_branch}
                  onInput={(event) =>
                    patch("default_branch", (event.target as HTMLInputElement).value)
                  }
                />
              </Field>
              <Field label="Branch prefix">
                <input
                  value={form.branch_prefix}
                  onInput={(event) =>
                    patch("branch_prefix", (event.target as HTMLInputElement).value)
                  }
                />
              </Field>
            </div>
          </SetupPanel>

          <SetupPanel title="Validation" right="commands">
            <div class="op-setup-fields">
              <Field label="Test command">
                <input
                  value={form.test_command}
                  placeholder="npm test"
                  onInput={(event) =>
                    patch("test_command", (event.target as HTMLInputElement).value)
                  }
                />
              </Field>
              <Field label="Lint command">
                <input
                  value={form.lint_command}
                  placeholder="npm run lint"
                  onInput={(event) =>
                    patch("lint_command", (event.target as HTMLInputElement).value)
                  }
                />
              </Field>
              <Field label="Build command">
                <input
                  value={form.build_command}
                  placeholder="npm run build"
                  onInput={(event) =>
                    patch("build_command", (event.target as HTMLInputElement).value)
                  }
                />
              </Field>
              <Field label="E2E command">
                <input
                  value={form.e2e_command}
                  placeholder="npm run e2e"
                  onInput={(event) =>
                    patch("e2e_command", (event.target as HTMLInputElement).value)
                  }
                />
              </Field>
            </div>
          </SetupPanel>

          {(selectedProfileFields.length > 0 ||
            selectedEnvSettings.length > 0 ||
            credentialFields.length > 0) && (
            <SetupPanel title={`${label(form.platform_profile)} settings`} right="local">
              <div class="op-setup-fields">
                {selectedProfileFields.map((field) => (
                  <Field key={field.key} label={field.label}>
                    <input
                      value={profilePlatformSettings[field.key] ?? ""}
                      placeholder={field.placeholder}
                      onInput={(event) =>
                        setProfilePlatformSettings((current) => ({
                          ...current,
                          [field.key]: (event.target as HTMLInputElement).value,
                        }))
                      }
                    />
                  </Field>
                ))}
                {credentialFields.map((setting) => (
                  <Field key={setting.key} label={setting.label}>
                    <input
                      type={setting.secret ? "password" : "text"}
                      value={platformSettings[setting.key] ?? ""}
                      placeholder={setting.placeholder || setting.key}
                      onInput={(event) =>
                        setPlatformSettings((current) => ({
                          ...current,
                          [setting.key]: (event.target as HTMLInputElement).value,
                        }))
                      }
                    />
                  </Field>
                ))}
                {selectedEnvSettings.map((setting) => (
                  <Field
                    key={setting.key}
                    label={`${setting.label}${setting.present ? " (saved)" : ""}`}
                  >
                    <input
                      type={setting.secret ? "password" : "text"}
                      value={platformSettings[setting.key] ?? ""}
                      placeholder={setting.default || setting.key}
                      onInput={(event) =>
                        setPlatformSettings((current) => ({
                          ...current,
                          [setting.key]: (event.target as HTMLInputElement).value,
                        }))
                      }
                    />
                  </Field>
                ))}
              </div>
            </SetupPanel>
          )}

          <div class="op-setup-actions is-final">
            <Button
              variant="primary"
              disabled={state === "busy" || !canSave(form)}
              onClick={save}
            >
              Save project setup
            </Button>
          </div>
        </div>

        <aside class="op-project-rail">
          <SetupFacts inspect={inspect} />
          <NoteList title="Readiness" notes={saveResult?.readiness ?? inspect?.notes ?? []} />
          <ExistingProfiles options={options.data} />
        </aside>
      </section>
    </>
  );
}

function SetupPanel({
  title,
  right,
  children,
}: {
  title: string;
  right: string;
  children: ComponentChildren;
}) {
  return (
    <section class="op-setup-panel">
      <SectionHeader label={title} right={right} />
      {children}
    </section>
  );
}

function Field({
  label,
  wide,
  children,
}: {
  label: string;
  wide?: boolean;
  children: ComponentChildren;
}) {
  return (
    <label class={`op-setup-field${wide ? " is-wide" : ""}`}>
      <span>{label}</span>
      {children}
    </label>
  );
}

function SetupFacts({ inspect }: { inspect: ProjectSetupInspectResponse | null }) {
  return (
    <section class="op-setup-facts">
      <SectionHeader label="Directory facts" right={inspect?.is_git_repo ? "git" : "local"} />
      {!inspect ? (
        <div class="op-rail-log-conn">Inspect a directory to prefill this setup.</div>
      ) : (
        <div class="op-setup-kv">
          <Fact label="Path" value={inspect.path} />
          <Fact label="Git" value={inspect.is_git_repo ? "yes" : "no"} />
          <Fact label="Branch" value={inspect.git_branch || "—"} />
          <Fact label="Remote" value={inspect.git_remote || "—"} />
          <Fact label="GitHub" value={inspect.github_repo || "—"} />
          <Fact label="Detected type" value={label(inspect.detected_platform || "generic")} />
          <Fact
            label="Validation"
            value={(inspect.detected.validation_commands ?? []).join(", ") || "—"}
          />
        </div>
      )}
    </section>
  );
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div class="op-setup-fact">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function NoteList({ title, notes }: { title: string; notes: ProjectSetupNote[] }) {
  return (
    <section class="op-setup-facts">
      <SectionHeader label={title} right={`${notes.length}`} />
      {notes.length === 0 ? (
        <div class="op-rail-log-conn">No notes yet.</div>
      ) : (
        <div class="op-setup-notes">
          {notes.map((note) => (
            <div class="op-setup-note" key={`${note.severity}-${note.message}`}>
              <Pill tone={tone(note.severity)}>{note.severity}</Pill>
              <strong>{note.message}</strong>
              <span>{note.recommendation}</span>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function ExistingProfiles({ options }: { options?: ProjectSetupOptionsResponse }) {
  const profiles = options?.profiles ?? [];
  return (
    <section class="op-setup-facts">
      <SectionHeader label="Configured profiles" right={`${profiles.length}`} />
      {profiles.length === 0 ? (
        <div class="op-rail-log-conn">No client profiles configured.</div>
      ) : (
        <div class="op-setup-profile-list">
          {profiles.slice(0, 12).map((profile) => (
            <div class="op-setup-profile" key={profile.id}>
              <strong>{profile.id}</strong>
              <span>{label(profile.platform_profile || "generic")}</span>
              <span>{profile.repo_path || "no local path"}</span>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function canSave(form: FormState): boolean {
  return Boolean(
    form.profile_id.trim() && form.client_name.trim() && form.project_path.trim(),
  );
}

function tone(severity: ProjectSetupNote["severity"]) {
  if (severity === "ok") return "ok";
  if (severity === "error") return "err";
  if (severity === "warning") return "warn";
  return "cool";
}

function label(value: string): string {
  if (!value || value === "generic") return "Generic";
  if (value === "azure-repos") return "Azure Repos";
  return value
    .split(/[-_ ]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function credentialEnvFields({
  sourceIsGithub,
  sourceIsAzure,
  ticketIsAdo,
}: {
  sourceIsGithub: boolean;
  sourceIsAzure: boolean;
  ticketIsAdo: boolean;
}): LocalEnvField[] {
  const fields: LocalEnvField[] = [];
  if (sourceIsGithub) {
    fields.push(
      {
        key: "GITHUB_TOKEN",
        label: "GitHub token",
        secret: true,
        placeholder: "ghp_...",
      },
      {
        key: "AGENT_GH_TOKEN",
        label: "Agent GitHub token",
        secret: true,
        placeholder: "optional dedicated PAT",
      },
    );
  }
  if (sourceIsAzure || ticketIsAdo) {
    fields.push({
      key: "ADO_PAT",
      label: "ADO PAT",
      secret: true,
      placeholder: "local-only token",
    });
  }
  if (!ticketIsAdo) {
    fields.push(
      {
        key: "JIRA_USER_EMAIL",
        label: "Jira user email",
        placeholder: "you@company.com",
      },
      {
        key: "JIRA_API_TOKEN",
        label: "Jira API token",
        secret: true,
        placeholder: "local-only token",
      },
    );
  }
  return fields;
}

async function postInspect(projectPath: string): Promise<ProjectSetupInspectResponse> {
  const res = await fetch("/api/operator/project-setup/inspect", {
    method: "POST",
    headers: fetchHeaders({
      Accept: "application/json",
      "Content-Type": "application/json",
    }),
    credentials: "same-origin",
    body: JSON.stringify({ project_path: projectPath }),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status}: ${readableErrorText(text)}`);
  }
  return (await res.json()) as ProjectSetupInspectResponse;
}

async function putSetup(
  form: FormState,
  env: Record<string, string>,
  platformSettings: Record<string, string>,
): Promise<ProjectSetupSaveResponse> {
  const res = await fetch("/api/operator/project-setup", {
    method: "PUT",
    headers: fetchHeaders({
      Accept: "application/json",
      "Content-Type": "application/json",
    }),
    credentials: "same-origin",
    body: JSON.stringify({
      ...form,
      env,
      platform_settings: platformSettings,
      actions: {
        create_directory: form.create_directory,
        init_git: form.init_git,
        create_github_repo: form.create_github_repo,
      },
    }),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status}: ${readableErrorText(text)}`);
  }
  return (await res.json()) as ProjectSetupSaveResponse;
}
