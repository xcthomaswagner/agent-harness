import { fireEvent, render, waitFor } from "@testing-library/preact";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TicketsView } from "../Tickets";

class FakeEventSource {
  onopen: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;

  constructor(public url: string) {
    window.setTimeout(() => this.onopen?.(new Event("open")), 0);
  }

  close() {}
}

describe("TicketsView", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    document.head.innerHTML = "";
  });

  it("sends the dashboard API key when removing a ticket trigger label", async () => {
    document.head.innerHTML = '<meta name="operator-api-key" content="sekret">';
    vi.stubGlobal("EventSource", FakeEventSource);

    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.startsWith("/api/operator/traces")) {
        return jsonResponse({
          traces: [
            {
              id: "HARN-1",
              title: "Test ticket",
              status: "in-flight",
              raw_status: "In Flight",
              hidden: false,
              lifecycle_state: "",
              state_reason: "",
              run_id: "trace-1",
              phase: "implementing",
              elapsed: "1m",
              started_at: "2026-05-04T12:00:00+00:00",
              pr_url: null,
              pipeline_mode: "",
              review_verdict: "",
              qa_result: "",
            },
          ],
          count: 1,
          offset: 0,
          limit: 200,
          include_hidden: false,
        });
      }
      if (url.endsWith("/agents")) {
        return jsonResponse({ agents: [] });
      }
      if (url.endsWith("/activity-summary")) {
        return jsonResponse({
          ticket_id: "HARN-1",
          summary: "",
          raw_event_count: 0,
          deduped_event_count: 0,
          teammates: [],
          highlights: [],
          warnings: [],
        });
      }
      if (url.endsWith("/trigger-label")) {
        return jsonResponse({ status: "accepted" }, { status: 200 });
      }
      return jsonResponse({}, { status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock);

    const { findByText } = render(<TicketsView />);
    fireEvent.click(await findByText("Remove Trigger"));

    await waitFor(() => {
      const removeCall = fetchMock.mock.calls.find(([input]) =>
        String(input).endsWith("/trigger-label"),
      );
      expect(removeCall).toBeTruthy();
      expect(removeCall?.[1]?.method).toBe("DELETE");
      expect(removeCall?.[1]?.credentials).toBe("same-origin");
      expect(removeCall?.[1]?.headers).toMatchObject({
        "X-API-Key": "sekret",
      });
    });
  });
});

function jsonResponse(data: unknown, init?: ResponseInit): Response {
  return new Response(JSON.stringify(data), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}
