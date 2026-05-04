import { render, waitFor } from "@testing-library/preact";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useFeed } from "../useFeed";

function FeedProbe({ url }: { url: string | null }) {
  const feed = useFeed<{ ok: boolean }>(url, { intervalMs: 0 });
  return (
    <div>
      <span data-testid="status">{feed.status}</span>
      <span data-testid="error">{feed.error ?? ""}</span>
    </div>
  );
}

describe("useFeed", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("surfaces JSON error detail from failed responses", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify({ detail: "Invalid admin token" }), {
          status: 401,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );

    const { getByTestId } = render(<FeedProbe url="/api/test" />);

    await waitFor(() => expect(getByTestId("status").textContent).toBe("error"));
    expect(getByTestId("error").textContent).toBe("401: Invalid admin token");
  });
});
