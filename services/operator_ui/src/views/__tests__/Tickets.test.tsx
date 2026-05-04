import { fireEvent, render, waitFor } from "@testing-library/preact";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ActivitySummaryPanel,
  TeamActivity,
  TicketsView,
} from "../Tickets";
import { traceCountsFromResponse } from "../format";

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
          status_counts: {
            all: 1,
            "in-flight": 1,
            stuck: 0,
            queued: 0,
            done: 0,
            hidden: 0,
          },
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
      if (url.endsWith("/readiness")) {
        return jsonResponse(emptyReadiness("HARN-1"));
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

  it("requests hidden tickets only after the operator enables them", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/operator/traces")) {
        return jsonResponse({
          traces: [],
          count: 0,
          status_counts: {
            all: 0,
            "in-flight": 0,
            stuck: 0,
            queued: 0,
            done: 0,
            hidden: 0,
          },
          offset: 0,
          limit: 200,
          include_hidden: url.includes("include_hidden=true"),
        });
      }
      return jsonResponse({}, { status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock);

    const { findByText } = render(<TicketsView />);
    fireEvent.click(await findByText("Show hidden"));

    await waitFor(() => {
      const urls = fetchMock.mock.calls.map(([input]) => String(input));
      expect(urls.some((url) => url.includes("include_hidden=false"))).toBe(true);
      expect(urls.some((url) => url.includes("include_hidden=true"))).toBe(true);
    });
  });

  it("derives status counts when the backend omits status_counts", async () => {
    const counts = traceCountsFromResponse({
      traces: [
        { ...traceSummary("HARN-1"), status: "done" },
        { ...traceSummary("HARN-2"), status: "stuck" },
        { ...traceSummary("HARN-3"), status: "done" },
      ],
      count: 3,
      offset: 0,
      limit: 500,
      include_hidden: false,
    });

    expect(counts.all).toBe(3);
    expect(counts.done).toBe(2);
    expect(counts.stuck).toBe(1);
    expect(counts["in-flight"]).toBe(0);
  });

  it("uses the unfiltered response for filter chip counts", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/operator/traces") && url.includes("status=in-flight")) {
        return jsonResponse({
          traces: [],
          count: 0,
          offset: 0,
          limit: 200,
          include_hidden: false,
        });
      }
      if (url.startsWith("/api/operator/traces")) {
        return jsonResponse({
          traces: [
            { ...traceSummary("HARN-1"), status: "done" },
            { ...traceSummary("HARN-2"), status: "stuck" },
            { ...traceSummary("HARN-3"), status: "done" },
          ],
          count: 3,
          offset: 0,
          limit: 500,
          include_hidden: false,
        });
      }
      return jsonResponse({}, { status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock);

    const { findByText } = render(<TicketsView />);

    expect(await findByText("All")).toBeTruthy();
    expect(await findByText("3")).toBeTruthy();
    expect(await findByText("Done")).toBeTruthy();
    expect(await findByText("2")).toBeTruthy();
    expect(await findByText("Stuck")).toBeTruthy();
    expect(await findByText("1")).toBeTruthy();
  });

  it("shows backend detail when trigger removal fails", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/operator/traces")) {
        return jsonResponse({
          traces: [
            {
              id: "HARN-2",
              title: "Broken trigger",
              status: "in-flight",
              raw_status: "In Flight",
              hidden: false,
              lifecycle_state: "",
              state_reason: "",
              run_id: "trace-2",
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
          status_counts: {
            all: 1,
            "in-flight": 1,
            stuck: 0,
            queued: 0,
            done: 0,
            hidden: 0,
          },
          offset: 0,
          limit: 200,
          include_hidden: false,
        });
      }
      if (url.endsWith("/agents")) return jsonResponse({ agents: [] });
      if (url.endsWith("/activity-summary")) {
        return jsonResponse({
          ticket_id: "HARN-2",
          summary: "",
          raw_event_count: 0,
          deduped_event_count: 0,
          teammates: [],
          highlights: [],
          warnings: [],
        });
      }
      if (url.endsWith("/readiness")) return jsonResponse(emptyReadiness("HARN-2"));
      if (url.endsWith("/trigger-label")) {
        return jsonResponse({ detail: "Adapter token expired" }, { status: 500 });
      }
      return jsonResponse({}, { status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock);

    const { findByText } = render(<TicketsView />);
    fireEvent.click(await findByText("Remove Trigger"));

    expect(await findByText(/Remove trigger failed: Adapter token expired/))
      .toBeTruthy();
  });

  it("surfaces client readiness warnings in the ticket rail", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/operator/traces")) {
        return jsonResponse({
          traces: [traceSummary("HARN-READY")],
          count: 1,
          status_counts: {
            all: 1,
            "in-flight": 1,
            stuck: 0,
            queued: 0,
            done: 0,
            hidden: 0,
          },
          offset: 0,
          limit: 200,
          include_hidden: false,
        });
      }
      if (url.endsWith("/agents")) return jsonResponse({ agents: [] });
      if (url.endsWith("/activity-summary")) {
        return jsonResponse({
          ticket_id: "HARN-READY",
          summary: "",
          raw_event_count: 0,
          deduped_event_count: 0,
          teammates: [],
          highlights: [],
          warnings: [],
        });
      }
      if (url.endsWith("/readiness")) {
        return jsonResponse({
          ...emptyReadiness("HARN-READY"),
          available: true,
          source: "worktree",
          warning_count: 1,
          warnings: [
            {
              id: "next_tailwind_not_configured",
              area: "frontend",
              severity: "warning",
              message: "Tailwind is referenced but not configured.",
              recommendation: "Use existing CSS or configure Tailwind first.",
            },
          ],
        });
      }
      return jsonResponse({}, { status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock);

    const { findByText } = render(<TicketsView />);

    expect(await findByText("Client readiness")).toBeTruthy();
    expect(await findByText("Tailwind is referenced but not configured."))
      .toBeTruthy();
    expect(await findByText("Use existing CSS or configure Tailwind first."))
      .toBeTruthy();
  });

  it("distinguishes panel fetch errors from empty activity", () => {
    const { getByText } = render(
      <div>
        <ActivitySummaryPanel
          data={undefined}
          state="error"
          error="500: summary unavailable"
        />
        <TeamActivity
          agents={undefined}
          state="error"
          error="500: roster unavailable"
        />
      </div>,
    );

    expect(getByText("Failed to load activity summary: 500: summary unavailable"))
      .toBeTruthy();
    expect(getByText("Failed to load team activity: 500: roster unavailable"))
      .toBeTruthy();
  });
});

function jsonResponse(data: unknown, init?: ResponseInit): Response {
  return new Response(JSON.stringify(data), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

function emptyReadiness(ticketId: string) {
  return {
    ticket_id: ticketId,
    available: false,
    source: "",
    generated_by: "",
    client_profile: "",
    is_next: false,
    warning_count: 0,
    warnings: [],
  };
}

function traceSummary(id: string) {
  return {
    id,
    title: "Test ticket",
    status: "in-flight",
    raw_status: "In Flight",
    hidden: false,
    lifecycle_state: "",
    state_reason: "",
    run_id: `trace-${id}`,
    phase: "implementing",
    elapsed: "1m",
    started_at: "2026-05-04T12:00:00+00:00",
    pr_url: null,
    pipeline_mode: "",
    review_verdict: "",
    qa_result: "",
  };
}
