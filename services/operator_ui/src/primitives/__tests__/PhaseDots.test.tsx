import { render } from "@testing-library/preact";
import { describe, expect, it } from "vitest";
import { PhaseDots } from "../PhaseDots";

describe("PhaseDots", () => {
  it("renders one dot per phase", () => {
    const { container } = render(
      <PhaseDots phases={["done", "done", "active", "pending", "pending"]} />,
    );
    expect(container.querySelectorAll(".op-phase-dot")).toHaveLength(5);
  });

  it("applies state classes", () => {
    const { container } = render(
      <PhaseDots phases={["done", "active", "fail"]} />,
    );
    const dots = container.querySelectorAll(".op-phase-dot");
    expect(dots[0]?.classList.contains("is-done")).toBe(true);
    expect(dots[1]?.classList.contains("is-active")).toBe(true);
    expect(dots[2]?.classList.contains("is-fail")).toBe(true);
  });

  it("honors shimmer=false via data attribute", () => {
    const { container } = render(
      <PhaseDots phases={["active"]} shimmer={false} />,
    );
    const root = container.querySelector(".op-phase-dots");
    expect(root?.getAttribute("data-shimmer")).toBe("off");
  });
});
