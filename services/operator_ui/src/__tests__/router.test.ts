import { describe, expect, it, vi } from "vitest";
import { href, parseRoute } from "../router";

describe("parseRoute", () => {
  it("home", () => {
    expect(parseRoute("/operator/")).toEqual({ name: "home" });
    expect(parseRoute("/operator")).toEqual({ name: "home" });
  });

  it("tickets / traces / learning", () => {
    expect(parseRoute("/operator/tickets")).toEqual({ name: "tickets" });
    expect(parseRoute("/operator/traces")).toEqual({ name: "traces" });
    expect(parseRoute("/operator/learning")).toEqual({ name: "learning" });
  });

  it("trace-detail with url-encoded id", () => {
    expect(parseRoute("/operator/traces/HARN-2043")).toEqual({
      name: "trace-detail",
      id: "HARN-2043",
    });
    expect(parseRoute("/operator/traces/PROJ%2F-99")).toEqual({
      name: "trace-detail",
      id: "PROJ/-99",
    });
  });

  it("pr-detail", () => {
    expect(parseRoute("/operator/pr/PR-1184")).toEqual({
      name: "pr-detail",
      id: "PR-1184",
    });
  });

  it("autonomy with optional profile", () => {
    expect(parseRoute("/operator/autonomy")).toEqual({ name: "autonomy" });
    expect(parseRoute("/operator/autonomy/xcsf30")).toEqual({
      name: "autonomy",
      profile: "xcsf30",
    });
  });

  it("trailing slash tolerated", () => {
    expect(parseRoute("/operator/autonomy/")).toEqual({ name: "autonomy" });
    expect(parseRoute("/operator/traces/HARN-1/")).toEqual({
      name: "trace-detail",
      id: "HARN-1",
    });
  });

  it("unknown path → not-found", () => {
    expect(parseRoute("/operator/made-up")).toEqual({ name: "not-found" });
  });
});

describe("href", () => {
  it("round-trips every route shape", () => {
    const routes = [
      { name: "home" } as const,
      { name: "tickets" } as const,
      { name: "traces" } as const,
      { name: "trace-detail", id: "HARN-42" } as const,
      { name: "autonomy" } as const,
      { name: "autonomy", profile: "xcsf30" } as const,
      { name: "learning" } as const,
      { name: "pr-detail", id: "PR-9" } as const,
    ];
    for (const r of routes) {
      expect(parseRoute(href(r))).toEqual(r);
    }
  });

  it("encodes special characters in ids", () => {
    expect(href({ name: "trace-detail", id: "a/b" })).toBe(
      "/operator/traces/a%2Fb",
    );
  });

  it("preserves bootstrapped api key in operator links", async () => {
    vi.resetModules();
    document.head.innerHTML = '<meta name="operator-api-key" content="sekret">';
    const freshRouter = await import("../router");
    expect(freshRouter.href({ name: "traces" })).toBe(
      "/operator/traces?api_key=sekret",
    );
    expect(freshRouter.href({ name: "trace-detail", id: "HARN-1" })).toBe(
      "/operator/traces/HARN-1?api_key=sekret",
    );
    document.head.innerHTML = "";
  });
});
