import { fireEvent, render, waitFor } from "@testing-library/preact";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ProjectSetupView } from "../ProjectSetup";

describe("ProjectSetupView", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    document.head.innerHTML = "";
  });

  it("inspects a local directory and saves a project profile with env settings", async () => {
    document.head.innerHTML = '<meta name="operator-api-key" content="sekret">';
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/operator/project-setup/options") {
        return jsonResponse(optionsResponse());
      }
      if (url === "/api/operator/project-setup/inspect") {
        expect(init?.method).toBe("POST");
        expect(init?.headers).toMatchObject({ "X-API-Key": "sekret" });
        expect(JSON.parse(String(init?.body))).toMatchObject({
          project_path: "/tmp/widgets",
        });
        return jsonResponse({
          input_path: "/tmp/widgets",
          path: "/tmp/widgets",
          exists: true,
          is_dir: true,
          git_root: "/tmp/widgets",
          is_git_repo: true,
          git_branch: "main",
          git_remote: "git@github.com:acme/widgets.git",
          github_repo: "acme/widgets",
          suggested_profile_id: "widgets",
          suggested_client_name: "Widgets",
          detected_platform: "contentstack",
          detected: {
            validation_commands: ["pnpm test", "pnpm build"],
            frameworks: ["Next.js"],
          },
          matching_profiles: [],
          notes: [],
        });
      }
      if (url === "/api/operator/project-setup") {
        expect(init?.method).toBe("PUT");
        expect(init?.headers).toMatchObject({ "X-API-Key": "sekret" });
        const body = JSON.parse(String(init?.body));
        expect(body).toMatchObject({
          profile_id: "widgets",
          client_name: "Widgets",
          project_path: "/tmp/widgets",
          platform_profile: "contentstack",
          github_repo: "acme/widgets",
          test_command: "pnpm test",
          build_command: "pnpm build",
        });
        expect(body.env).toMatchObject({
          CONTENTSTACK_REGION: "NA",
          GITHUB_TOKEN: "ghp_test",
        });
        expect(body.platform_settings).toMatchObject({
          frontend_framework: "Next.js App Router",
        });
        return jsonResponse({
          saved: true,
          profile_id: "widgets",
          profile_path: "/repo/runtime/client-profiles/widgets.yaml",
          project_path: "/tmp/widgets",
          platform_profile: "contentstack",
          source_control_type: "github",
          github_repo_created: false,
          env_written: ["CONTENTSTACK_REGION"],
          readiness: [
            {
              severity: "ok",
              message: "Profile is ready for harness runs from this local directory.",
              recommendation: "Add the trigger label on a matching ticket.",
            },
          ],
        });
      }
      return jsonResponse({}, { status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock);

    const { container, findByText } = render(<ProjectSetupView />);

    const pathInput = await findInput(container, "Project directory");
    fireEvent.input(pathInput, { target: { value: "/tmp/widgets" } });
    fireEvent.click(await findByText("Inspect directory"));

    await waitFor(() => {
      expect((findInputSync(container, "Profile ID") as HTMLInputElement).value).toBe(
        "widgets",
      );
      expect((findInputSync(container, "GitHub repo") as HTMLInputElement).value).toBe(
        "acme/widgets",
      );
    });

    fireEvent.input(findInputSync(container, "Ticket prefix"), {
      target: { value: "WID" },
    });
    fireEvent.input(findInputSync(container, "Frontend framework"), {
      target: { value: "Next.js App Router" },
    });
    fireEvent.input(findInputSync(container, "Region"), {
      target: { value: "NA" },
    });
    fireEvent.input(findInputSync(container, "GitHub token"), {
      target: { value: "ghp_test" },
    });
    fireEvent.click(await findByText("Save project setup"));

    expect(await findByText(/Saved widgets/)).toBeTruthy();
    expect(await findByText(/Profile is ready/)).toBeTruthy();
  });
});

function optionsResponse() {
  return {
    platforms: ["generic", "contentstack", "salesforce", "sitecore"],
    profiles: [],
    ticket_sources: ["jira", "ado"],
    source_controls: ["github", "azure-repos"],
    platform_settings: {
      generic: [],
      salesforce: [],
      sitecore: [],
      contentstack: [
        {
          key: "CONTENTSTACK_REGION",
          label: "Region",
          secret: false,
          required: true,
          help: "",
          default: "NA",
          present: false,
        },
      ],
    },
    profile_platform_fields: {
      generic: [],
      salesforce: [],
      sitecore: [],
      contentstack: [
        {
          key: "frontend_framework",
          label: "Frontend framework",
          placeholder: "Next.js App Router",
        },
      ],
    },
  };
}

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: init.status ?? 200,
    headers: { "Content-Type": "application/json", ...(init.headers ?? {}) },
  });
}

async function findInput(container: ParentNode, label: string): Promise<HTMLInputElement> {
  await waitFor(() => expect(findInputSync(container, label)).toBeTruthy());
  return findInputSync(container, label) as HTMLInputElement;
}

function findInputSync(container: ParentNode, label: string): HTMLInputElement | HTMLSelectElement {
  const fields = [...container.querySelectorAll("label")];
  const field = fields.find((candidate) => candidate.textContent?.includes(label));
  if (!field) throw new Error(`field not found: ${label}`);
  const input = field.querySelector("input, select");
  if (!(input instanceof HTMLInputElement || input instanceof HTMLSelectElement)) {
    throw new Error(`input not found: ${label}`);
  }
  return input;
}
