import { fireEvent, render } from "@testing-library/preact";
import { describe, expect, it, vi } from "vitest";
import { Table } from "../Table";

interface Row {
  id: string;
  name: string;
  count: number;
}

const columns = [
  { key: "id", label: "ID", render: (r: Row) => r.id },
  { key: "name", label: "Name", render: (r: Row) => r.name },
  { key: "count", label: "Count", render: (r: Row) => r.count, numeric: true },
] as const;

describe("Table", () => {
  it("renders header + rows", () => {
    const rows: Row[] = [
      { id: "T-1", name: "alpha", count: 4 },
      { id: "T-2", name: "bravo", count: 11 },
    ];
    const { container, getByText } = render(
      <Table columns={columns} rows={rows} rowKey={(r) => r.id} />,
    );
    expect(getByText("ID")).toBeTruthy();
    expect(getByText("alpha")).toBeTruthy();
    expect(container.querySelectorAll("tbody tr")).toHaveLength(2);
  });

  it("marks live rows with is-live class", () => {
    const rows: Row[] = [
      { id: "T-1", name: "alpha", count: 4 },
      { id: "T-2", name: "bravo", count: 11 },
    ];
    const { container } = render(
      <Table
        columns={columns}
        rows={rows}
        rowKey={(r) => r.id}
        isLive={(r) => r.id === "T-1"}
      />,
    );
    const bodyRows = container.querySelectorAll("tbody tr");
    expect(bodyRows[0]?.classList.contains("is-live")).toBe(true);
    expect(bodyRows[1]?.classList.contains("is-live")).toBe(false);
  });

  it("calls onRowClick with the row data", () => {
    const rows: Row[] = [{ id: "T-1", name: "alpha", count: 4 }];
    const onRowClick = vi.fn();
    const { container } = render(
      <Table
        columns={columns}
        rows={rows}
        rowKey={(r) => r.id}
        onRowClick={onRowClick}
      />,
    );
    fireEvent.click(container.querySelector("tbody tr")!);
    expect(onRowClick).toHaveBeenCalledWith(rows[0]);
  });

  it("renders an empty state when there are no rows", () => {
    const { getByText } = render(
      <Table columns={columns} rows={[]} rowKey={(r) => r.id} empty="Nothing here" />,
    );
    expect(getByText("Nothing here")).toBeTruthy();
  });

  it("applies is-num class on numeric cells", () => {
    const rows: Row[] = [{ id: "T-1", name: "alpha", count: 4 }];
    const { container } = render(
      <Table columns={columns} rows={rows} rowKey={(r) => r.id} />,
    );
    const countCells = container.querySelectorAll("tbody td");
    // The third column (count) is numeric.
    expect(countCells[2]?.classList.contains("is-num")).toBe(true);
  });

  it("sorts by any header and toggles direction", () => {
    const rows: Row[] = [
      { id: "T-2", name: "bravo", count: 11 },
      { id: "T-1", name: "alpha", count: 4 },
      { id: "T-3", name: "charlie", count: 7 },
    ];
    const { container, getByRole } = render(
      <Table columns={columns} rows={rows} rowKey={(r) => r.id} />,
    );

    fireEvent.click(getByRole("button", { name: /Name/ }));
    expect(
      [...container.querySelectorAll("tbody tr td:first-child")].map(
        (td) => td.textContent,
      ),
    ).toEqual(["T-1", "T-2", "T-3"]);
    expect(
      container.querySelector("th[aria-sort='ascending']")?.textContent,
    ).toContain("Name");

    fireEvent.click(getByRole("button", { name: /Name/ }));
    expect(
      [...container.querySelectorAll("tbody tr td:first-child")].map(
        (td) => td.textContent,
      ),
    ).toEqual(["T-3", "T-2", "T-1"]);
    expect(
      container.querySelector("th[aria-sort='descending']")?.textContent,
    ).toContain("Name");
  });

  it("sorts rendered vnode text when no direct row key exists", () => {
    const rows: Row[] = [
      { id: "T-1", name: "zulu", count: 4 },
      { id: "T-2", name: "alpha", count: 11 },
    ];
    const vnodeColumns = [
      {
        key: "display",
        label: "Display",
        render: (r: Row) => <span>{r.name}</span>,
      },
    ];
    const { container, getByRole } = render(
      <Table columns={vnodeColumns} rows={rows} rowKey={(r) => r.id} />,
    );

    fireEvent.click(getByRole("button", { name: /Display/ }));
    expect(
      [...container.querySelectorAll("tbody tr td:first-child")].map(
        (td) => td.textContent,
      ),
    ).toEqual(["alpha", "zulu"]);
  });
});
