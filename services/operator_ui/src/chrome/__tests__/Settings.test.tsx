import { fireEvent, render, waitFor } from "@testing-library/preact";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Settings } from "../Settings";

const policy = {
  version: 1,
  source: "default",
  model_options: ["claude-sonnet-4-5", "claude-opus-4-5"],
  reasoning_options: ["low", "medium", "high"],
  roles: [
    {
      role: "team_lead",
      label: "Team Lead",
      model: "claude-opus-4-5",
      reasoning: "high",
    },
  ],
};

describe("Settings", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("shows backend detail when saving model policy fails", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(policy))
      .mockResolvedValueOnce(
        jsonResponse({ detail: "Invalid model policy" }, { status: 400 }),
      );
    vi.stubGlobal("fetch", fetchMock);

    const { findByText, getByText } = render(<Settings onClose={() => {}} />);

    fireEvent.click(await findByText("Save"));

    await waitFor(() => {
      expect(getByText(/Model policy unavailable: 400: Invalid model policy/))
        .toBeTruthy();
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
