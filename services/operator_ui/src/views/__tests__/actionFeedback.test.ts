import { describe, expect, it } from "vitest";
import { parseJsonObject, readableError, readableErrorText } from "../actionFeedback";

describe("actionFeedback", () => {
  it("extracts the most useful error field", () => {
    expect(readableError({ detail: "Invalid admin token" }, "fallback")).toBe(
      "Invalid admin token",
    );
    expect(readableError({ error: "bad transition" }, "fallback")).toBe(
      "bad transition",
    );
    expect(readableError({ status_reason: "still proposed" }, "fallback")).toBe(
      "still proposed",
    );
  });

  it("falls back to response text when JSON parsing fails", () => {
    expect(parseJsonObject("not-json")).toBeNull();
    expect(readableErrorText("plain failure")).toBe("plain failure");
  });
});
