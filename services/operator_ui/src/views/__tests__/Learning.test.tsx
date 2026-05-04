import { fireEvent, render, waitFor } from "@testing-library/preact";
import { afterEach, describe, expect, it, vi } from "vitest";
import { LearningView } from "../Learning";

const candidate = {
  lesson_id: "LSN-1",
  detector_name: "mcp_drift",
  pattern_key: "expired_token",
  scope_key: "contentstack",
  client_profile: "xcsf30",
  platform_profile: "contentstack",
  status: "snoozed",
  status_reason: "",
  severity: "warn",
  frequency: 2,
  first_seen_at: "2026-05-01T00:00:00Z",
  last_seen_at: "2026-05-04T00:00:00Z",
  updated_at: "2026-05-04T00:00:00Z",
  pr_url: null,
  merged_commit_sha: null,
  proposed_delta_json: "{}",
  next_review_at: "2026-05-11T00:00:00Z",
};

describe("LearningView", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("allows snoozed lessons to be rejected", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.startsWith("/api/learning/candidates/LSN-1/reject")) {
        return jsonResponse({ ...candidate, status: "rejected" });
      }
      if (url.startsWith("/api/learning/candidates")) {
        return jsonResponse({
          candidates: [candidate],
          count: 1,
          total: 1,
          limit: 200,
          offset: 0,
        });
      }
      if (url.startsWith("/api/operator/lessons/counts")) {
        return jsonResponse({
          counts: {
            proposed: 0,
            draft_ready: 0,
            approved: 0,
            applied: 0,
            snoozed: 1,
            rejected: 0,
            reverted: 0,
            stale: 0,
          },
        });
      }
      return jsonResponse({}, { status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock);

    const { findByText } = render(<LearningView />);
    fireEvent.click(await findByText("Reject"));

    await waitFor(() => {
      const rejectCall = fetchMock.mock.calls.find(([input]) =>
        String(input).includes("/api/learning/candidates/LSN-1/reject"),
      );
      expect(rejectCall).toBeTruthy();
      expect(rejectCall?.[1]?.method).toBe("POST");
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
