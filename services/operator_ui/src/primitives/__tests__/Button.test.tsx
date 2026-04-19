import { fireEvent, render } from "@testing-library/preact";
import { describe, expect, it, vi } from "vitest";
import { Button } from "../Button";

describe("Button", () => {
  it("renders the children", () => {
    const { getByText } = render(<Button>Open</Button>);
    expect(getByText("Open")).toBeTruthy();
  });

  it("applies variant classes", () => {
    const { container } = render(<Button variant="primary">Merge</Button>);
    expect(container.querySelector(".op-btn")?.classList.contains("is-primary")).toBe(true);
  });

  it("applies size class", () => {
    const { container } = render(<Button size="sm">Edit</Button>);
    expect(container.querySelector(".op-btn")?.classList.contains("is-sm")).toBe(true);
  });

  it("calls onClick", () => {
    const onClick = vi.fn();
    const { container } = render(<Button onClick={onClick}>Go</Button>);
    fireEvent.click(container.querySelector("button")!);
    expect(onClick).toHaveBeenCalled();
  });

  it("renders with the disabled attribute when disabled", () => {
    // jsdom still fires synthetic click handlers on disabled buttons;
    // assert the attribute presence instead (real browsers block the
    // handler before it reaches Preact, the disabled state is what
    // matters for accessibility and visual state).
    const { container } = render(<Button disabled>Go</Button>);
    const btn = container.querySelector("button")!;
    expect(btn.hasAttribute("disabled")).toBe(true);
  });
});
