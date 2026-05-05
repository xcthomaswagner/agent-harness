import { fireEvent, render, waitFor } from "@testing-library/preact";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RepoWorkflowView } from "../RepoWorkflow";

describe("RepoWorkflowView", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    document.head.innerHTML = "";
  });

  it("generates editable workflow drafts and saves with the dashboard API key", async () => {
    document.head.innerHTML = '<meta name="operator-api-key" content="sekret">';
    let draftCalls = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/operator/repo-workflow/options") {
        return jsonResponse({
          profiles: [
            {
              client_profile: "harness-test-client",
              platform_profile: "contentstack",
              repo_path: "/tmp/harness-test-client",
              repo_exists: true,
              workflow_exists: false,
            },
          ],
        });
      }
      if (url === "/api/operator/repo-workflow/draft") {
        draftCalls += 1;
        expect(init?.method).toBe("POST");
        expect(init?.headers).toMatchObject({ "X-API-Key": "sekret" });
        expect(JSON.parse(String(init?.body))).toMatchObject({
          client_profile: "harness-test-client",
          repo_path: "/tmp/harness-test-client",
        });
        return jsonResponse(draftResponse(draftCalls > 1));
      }
      if (url === "/api/operator/repo-workflow") {
        expect(init?.method).toBe("PUT");
        expect(init?.headers).toMatchObject({ "X-API-Key": "sekret" });
        expect(JSON.parse(String(init?.body))).toMatchObject({
          content: "# WORKFLOW.md\n\ncustom",
        });
        return jsonResponse({
          saved: true,
          repo_path: "/tmp/harness-test-client",
          workflow_path: "/tmp/harness-test-client/WORKFLOW.md",
          workflow_exists: true,
          bytes: 21,
          updated_at: "2026-05-05T12:00:00+00:00",
        });
      }
      return jsonResponse({}, { status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock);

    const { container, findByText } = render(<RepoWorkflowView />);

    await waitFor(() => {
      const input = container.querySelector("input") as HTMLInputElement;
      expect(input.value).toBe("/tmp/harness-test-client");
    });
    fireEvent.click(await findByText("Generate Draft"));

    await waitFor(() => {
      const textarea = container.querySelector("textarea") as HTMLTextAreaElement;
      expect(textarea.value).toContain("## Validation Commands");
    });

    const textarea = container.querySelector("textarea") as HTMLTextAreaElement;
    fireEvent.input(textarea, { target: { value: "# WORKFLOW.md\n\ncustom" } });
    fireEvent.click(await findByText("Save WORKFLOW.md"));

    await waitFor(() => {
      expect(fetchMock.mock.calls.some(([input]) => String(input) === "/api/operator/repo-workflow")).toBe(
        true,
      );
      expect(draftCalls).toBe(2);
    });
    expect(await findByText("Saved 21 bytes to /tmp/harness-test-client/WORKFLOW.md.")).toBeTruthy();
    expect(await findByText("present")).toBeTruthy();
  });

  it("switches to manual path mode when the repo path is edited", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/operator/repo-workflow/options") {
        return jsonResponse({
          profiles: [
            {
              client_profile: "harness-test-client",
              platform_profile: "contentstack",
              repo_path: "/tmp/harness-test-client",
              repo_exists: true,
              workflow_exists: false,
            },
          ],
        });
      }
      return jsonResponse({}, { status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock);

    const { container } = render(<RepoWorkflowView />);

    await waitFor(() => {
      const select = container.querySelector("select") as HTMLSelectElement;
      expect(select.value).toBe("harness-test-client");
    });

    const input = container.querySelector("input") as HTMLInputElement;
    fireEvent.input(input, { target: { value: "/tmp/manual-repo" } });

    await waitFor(() => {
      const select = container.querySelector("select") as HTMLSelectElement;
      expect(select.value).toBe("__manual__");
      expect(input.value).toBe("/tmp/manual-repo");
    });
  });
});

function draftResponse(saved = false) {
  return {
    repo_path: "/tmp/harness-test-client",
    client_profile: "harness-test-client",
    platform_profile: "contentstack",
    workflow_path: "/tmp/harness-test-client/WORKFLOW.md",
    workflow_exists: saved,
    existing_text: saved ? "# WORKFLOW.md\n\ncustom" : "",
    draft_text: "# WORKFLOW.md\n\n## Validation Commands\n\n```bash\nnpm run build\n```",
    detected: {
      repo_name: "harness-test-client",
      git_branch: "main",
      git_remote: "",
      package_manager: "npm",
      frameworks: ["Next.js", "ContentStack"],
      test_tools: ["Vitest"],
      ci_files: [".github/workflows/ci.yml"],
      docs: ["README.md"],
      env_examples: [".env.example"],
      validation_commands: ["npm run build"],
      package_json_count: 1,
    },
    evidence: [
      {
        area: "scripts",
        source: "package.json",
        message: "Detected build script",
        value: "next build",
      },
    ],
    warnings: [],
    validation: saved
      ? []
      : [
          {
            id: "workflow_missing",
            area: "workflow",
            severity: "info",
            message: "No WORKFLOW.md exists yet.",
            recommendation: "Generate a draft.",
          },
        ],
    generated_at: "2026-05-05T12:00:00+00:00",
  };
}

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: init.status ?? 200,
    headers: { "Content-Type": "application/json", ...(init.headers ?? {}) },
  });
}
