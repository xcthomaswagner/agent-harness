import { render } from "@testing-library/preact";
import { describe, expect, it } from "vitest";
import { Pill } from "../Pill";

describe("Pill", () => {
  it("renders the label", () => {
    const { getByText } = render(<Pill tone="active">In-flight</Pill>);
    expect(getByText("In-flight")).toBeTruthy();
  });

  it("applies the tone class", () => {
    const { container } = render(<Pill tone="ok">Done</Pill>);
    const el = container.querySelector(".op-pill");
    expect(el?.classList.contains("is-ok")).toBe(true);
  });

  it("renders the dot span", () => {
    const { container } = render(<Pill tone="err">Failed</Pill>);
    expect(container.querySelector(".op-pill-dot")).toBeTruthy();
  });
});
