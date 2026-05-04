import { render } from "@testing-library/preact";
import { describe, expect, it } from "vitest";
import { Wordmark } from "../BrandGlyph";

describe("Wordmark", () => {
  it("renders the brand with a real word space", () => {
    const { container, getByLabelText } = render(<Wordmark />);

    expect(getByLabelText("Agentic Harness")).toBeTruthy();
    expect(container.textContent).toBe("Agentic Harness");
  });
});
