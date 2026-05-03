import { fireEvent, render } from "@testing-library/preact";
import { describe, expect, it, vi } from "vitest";
import { Chip } from "../Chip";

describe("Chip", () => {
  it("renders label and count", () => {
    const { getByText, container } = render(
      <Chip label="In-flight" count={3} />,
    );
    expect(getByText("In-flight")).toBeTruthy();
    expect(container.querySelector(".op-chip-n")?.textContent).toBe("3");
  });

  it("applies is-on class and aria-pressed when on", () => {
    const { container } = render(<Chip label="Done" on />);
    const el = container.querySelector(".op-chip");
    expect(el?.classList.contains("is-on")).toBe(true);
    expect(el?.getAttribute("aria-pressed")).toBe("true");
  });

  it("calls onClick", () => {
    const onClick = vi.fn();
    const { container } = render(<Chip label="All" onClick={onClick} />);
    fireEvent.click(container.querySelector(".op-chip")!);
    expect(onClick).toHaveBeenCalled();
  });

  it("omits count badge when count is undefined", () => {
    const { container } = render(<Chip label="Done" />);
    expect(container.querySelector(".op-chip-n")).toBeNull();
  });
});
