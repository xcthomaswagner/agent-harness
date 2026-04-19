import { render } from "@testing-library/preact";
import { describe, expect, it } from "vitest";
import { Sparkline } from "../Sparkline";

describe("Sparkline", () => {
  it("renders nothing usable when fewer than 2 points", () => {
    const { container } = render(<Sparkline values={[0.5]} />);
    // SVG still renders but without a polyline.
    expect(container.querySelector("polyline")).toBeNull();
  });

  it("renders one polyline for a continuous series", () => {
    const { container } = render(
      <Sparkline values={[0.1, 0.2, 0.3, 0.4]} />,
    );
    const lines = container.querySelectorAll("polyline");
    expect(lines).toHaveLength(1);
  });

  it("breaks into multiple segments around null entries", () => {
    const { container } = render(
      <Sparkline values={[0.1, 0.2, null, 0.4, 0.5]} />,
    );
    const lines = container.querySelectorAll("polyline");
    expect(lines).toHaveLength(2);
  });

  it("uses currentColor so stroke inherits from parent", () => {
    const { container } = render(
      <Sparkline values={[0.1, 0.2, 0.3]} />,
    );
    const line = container.querySelector("polyline");
    expect(line?.getAttribute("stroke")).toBe("currentColor");
  });
});
