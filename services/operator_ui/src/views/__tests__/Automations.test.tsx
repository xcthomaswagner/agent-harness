import { fireEvent, render, waitFor } from "@testing-library/preact";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AutomationsView } from "../Automations";

describe("AutomationsView", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    document.head.innerHTML = "";
  });

  it("saves automation settings and can run a job now", async () => {
    document.head.innerHTML = '<meta name="operator-api-key" content="sekret">';
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/operator/automations") {
        return jsonResponse(automationsResponse());
      }
      if (url === "/api/operator/automations/pipeline_watcher") {
        expect(init?.method).toBe("PUT");
        expect(init?.headers).toMatchObject({ "X-API-Key": "sekret" });
        const body = JSON.parse(String(init?.body));
        expect(body).toMatchObject({
          enabled: true,
          scope: "all",
        });
        expect(body.config).toMatchObject({ dry_run: false });
        return jsonResponse({
          job: { ...automationsResponse().jobs[0], enabled: false },
        });
      }
      if (url === "/api/operator/automations/pipeline_watcher/run") {
        expect(init?.method).toBe("POST");
        expect(init?.headers).toMatchObject({ "X-API-Key": "sekret" });
        return jsonResponse({
          run: {
            id: 12,
            job_key: "pipeline_watcher",
            status: "succeeded",
            triggered_by: "operator",
            started_at: "2026-05-10T12:00:00+00:00",
            finished_at: "2026-05-10T12:00:01+00:00",
            duration_ms: 1000,
            summary: "0 stale active trace(s), 0 event(s) emitted",
            details: {},
            error: "",
          },
        });
      }
      return jsonResponse({}, { status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock);

    const { container, findByText } = render(<AutomationsView />);

    expect(await findByText("Pipeline watcher")).toBeTruthy();
    fireEvent.change(findSelect(container, "Interval"), { target: { value: "600" } });
    fireEvent.input(findInput(container, "Stale after minutes"), {
      target: { value: "45" },
    });
    fireEvent.click(await findByText("Save settings"));
    expect(await findByText("Settings saved.")).toBeTruthy();

    fireEvent.click(await findByText("Run now"));
    expect(await findByText(/0 stale active trace/)).toBeTruthy();
  });
});

function automationsResponse() {
  return {
    jobs: [
      {
        job_key: "pipeline_watcher",
        label: "Pipeline watcher",
        description: "Looks for stuck active traces.",
        enabled: true,
        interval_seconds: 300,
        scope: "all",
        config: {
          stale_after_minutes: 120,
          event_cooldown_minutes: 60,
          dry_run: false,
        },
        next_run_at: "2026-05-10T12:05:00+00:00",
        created_at: "2026-05-10T12:00:00+00:00",
        updated_at: "2026-05-10T12:00:00+00:00",
        last_run: null,
      },
    ],
    recent_events: [
      {
        id: 1,
        job_key: "pipeline_watcher",
        run_id: 7,
        severity: "warning",
        target_type: "trace",
        target_id: "RND-1",
        message: "RND-1 has no progress for 180m.",
        payload: {},
        created_at: "2026-05-10T12:00:00+00:00",
      },
    ],
    interval_options: [300, 600, 3600],
    profiles: [{ id: "demo", name: "Demo" }],
  };
}

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: init.status ?? 200,
    headers: { "Content-Type": "application/json", ...(init.headers ?? {}) },
  });
}

function findInput(container: ParentNode, label: string): HTMLInputElement {
  const field = [...container.querySelectorAll("label")].find((candidate) =>
    candidate.textContent?.includes(label),
  );
  if (!field) throw new Error(`label not found: ${label}`);
  const input = field.querySelector("input");
  if (!(input instanceof HTMLInputElement)) throw new Error(`input not found: ${label}`);
  return input;
}

function findSelect(container: ParentNode, label: string): HTMLSelectElement {
  const field = [...container.querySelectorAll("label")].find((candidate) =>
    candidate.textContent?.includes(label),
  );
  if (!field) throw new Error(`label not found: ${label}`);
  const select = field.querySelector("select");
  if (!(select instanceof HTMLSelectElement)) {
    throw new Error(`select not found: ${label}`);
  }
  return select;
}
