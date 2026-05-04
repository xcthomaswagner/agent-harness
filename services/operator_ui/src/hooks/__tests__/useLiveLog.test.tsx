import { render, waitFor } from "@testing-library/preact";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useLiveLog } from "../useLiveLog";

const sources: FakeEventSource[] = [];

class FakeEventSource {
  onopen: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;

  constructor(public url: string) {
    sources.push(this);
    window.setTimeout(() => this.onopen?.(new Event("open")), 0);
  }

  close() {}

  emit(data: unknown) {
    this.onmessage?.(
      new MessageEvent("message", { data: JSON.stringify(data) }),
    );
  }
}

function LiveLogProbe({ ticketId }: { ticketId: string | null }) {
  const log = useLiveLog(ticketId);
  return (
    <div>
      <span data-testid="state">{log.state}</span>
      <span data-testid="count">{log.entries.length}</span>
      <ol>
        {log.entries.map((entry) => (
          <li key={entry.event_id ?? entry.timestamp}>
            {entry.event_id}:{entry.text}
          </li>
        ))}
      </ol>
    </div>
  );
}

describe("useLiveLog", () => {
  afterEach(() => {
    sources.length = 0;
    vi.unstubAllGlobals();
    document.head.innerHTML = "";
  });

  it("dedupes replayed events after EventSource reconnects", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    document.head.innerHTML = '<meta name="operator-api-key" content="sekret">';

    const { getByTestId, getAllByRole } = render(
      <LiveLogProbe ticketId="HARN-1" />,
    );

    await waitFor(() => expect(sources).toHaveLength(1));
    const source = sources[0];
    if (!source) throw new Error("EventSource was not created");
    expect(source.url).toBe("/api/traces/HARN-1/stream?api_key=sekret");

    const event = {
      event_id: "event-1",
      kind: "text",
      teammate: "team-lead",
      timestamp: "2026-05-04T12:00:00Z",
      text: "Planning ticket",
    };
    source.emit(event);
    source.emit(event);
    source.emit({ ...event, event_id: "event-2", text: "Dispatching dev" });

    await waitFor(() => expect(getByTestId("count").textContent).toBe("2"));
    expect(getAllByRole("listitem").map((item) => item.textContent)).toEqual([
      "event-2:Dispatching dev",
      "event-1:Planning ticket",
    ]);
  });
});
