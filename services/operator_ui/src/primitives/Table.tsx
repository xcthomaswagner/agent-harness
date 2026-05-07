import type { ComponentChildren } from "preact";
import { useMemo, useState } from "preact/hooks";

type SortDirection = "asc" | "desc";
type SortValue = string | number | boolean | null | undefined;

interface TableColumn<Row> {
  key: string;
  /** Header label (mono uppercase). */
  label: string;
  /** Cell renderer. */
  render: (row: Row) => ComponentChildren;
  /** Optional stable sort value. Falls back to row[key] or rendered text. */
  sortValue?: (row: Row) => SortValue;
  /** Right-align numeric cells. */
  numeric?: boolean;
  /** Fixed width in CSS units. Leave unset for flex. */
  width?: string;
  /** Optional CSS class applied to matching header and cells. */
  className?: string;
}

interface TableProps<Row> {
  columns: readonly TableColumn<Row>[];
  rows: readonly Row[];
  /** Large row padding (14px vs 12px). */
  large?: boolean;
  /** Return true for a row that should render with the amber "live" wash. */
  isLive?: (row: Row) => boolean;
  /** Click handler on a row. */
  onRowClick?: (row: Row) => void;
  /** Key function for stable Preact list rendering. */
  rowKey: (row: Row) => string;
  /** Content shown when rows is empty. */
  empty?: ComponentChildren;
}

/**
 * Flat rule-bordered table. Header row mono uppercase, body rows
 * padded 12/14px, dim rule below. Subtle hover tint. `.is-live` rows
 * get the amber gradient wash.
 */
export function Table<Row>({
  columns,
  rows,
  large = false,
  isLive,
  onRowClick,
  rowKey,
  empty,
}: TableProps<Row>) {
  const [sort, setSort] = useState<{ key: string; direction: SortDirection } | null>(
    null,
  );

  const sortedRows = useMemo(() => {
    if (sort === null) return rows;
    const column = columns.find((col) => col.key === sort.key);
    if (!column) return rows;
    return rows
      .map((row, index) => ({ row, index }))
      .sort((a, b) => {
        const result = compareSortValues(
          sortValueForRow(column, a.row),
          sortValueForRow(column, b.row),
        );
        if (result === 0) return a.index - b.index;
        return sort.direction === "asc" ? result : -result;
      })
      .map((item) => item.row);
  }, [columns, rows, sort]);

  const toggleSort = (key: string) => {
    setSort((current) => {
      if (current?.key !== key) return { key, direction: "asc" };
      return { key, direction: current.direction === "asc" ? "desc" : "asc" };
    });
  };

  return (
    <table class={`op-tbl${large ? " is-lg" : ""}`}>
      <thead>
        <tr>
          {columns.map((col) => (
            <th
              key={col.key}
              style={col.width ? { width: col.width } : undefined}
              class={columnClass(col)}
              aria-sort={
                sort?.key === col.key
                  ? sort.direction === "asc"
                    ? "ascending"
                    : "descending"
                  : "none"
              }
            >
              <button
                type="button"
                class="op-tbl-sort"
                aria-label={col.label ? undefined : `Sort by ${col.key}`}
                onClick={() => toggleSort(col.key)}
              >
                <span>{col.label}</span>
                <span class="op-tbl-sort-mark" aria-hidden="true">
                  {sort?.key === col.key
                    ? sort.direction === "asc"
                      ? "▲"
                      : "▼"
                    : "↕"}
                </span>
              </button>
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {sortedRows.length === 0 ? (
          <tr>
            <td colSpan={columns.length} style={{ textAlign: "center", padding: "32px 12px", color: "var(--ink-500)" }}>
              {empty ?? "No rows"}
            </td>
          </tr>
        ) : (
          sortedRows.map((row) => (
            <tr
              key={rowKey(row)}
              class={isLive?.(row) ? "is-live" : undefined}
              onClick={
                onRowClick
                  ? (event) => {
                      if (isInteractiveTarget(event.target)) return;
                      onRowClick(row);
                    }
                  : undefined
              }
              style={onRowClick ? { cursor: "pointer" } : undefined}
            >
              {columns.map((col) => (
                <td
                  key={col.key}
                  class={columnClass(col)}
                  style={col.width ? { width: col.width } : undefined}
                >
                  {col.render(row)}
                </td>
              ))}
            </tr>
          ))
        )}
      </tbody>
    </table>
  );
}

function columnClass<Row>(column: TableColumn<Row>): string | undefined {
  return [column.numeric ? "is-num" : "", column.className ?? ""]
    .filter(Boolean)
    .join(" ") || undefined;
}

function sortValueForRow<Row>(column: TableColumn<Row>, row: Row): SortValue {
  if (column.sortValue) return column.sortValue(row);
  const keyed = valueAtKey(row, column.key);
  if (keyed !== undefined) return keyed;
  return textFromChildren(column.render(row));
}

function valueAtKey<Row>(row: Row, key: string): SortValue {
  if (row === null || typeof row !== "object") return undefined;
  const value = (row as Record<string, unknown>)[key];
  if (
    typeof value === "string" ||
    typeof value === "number" ||
    typeof value === "boolean" ||
    value === null
  ) {
    return value;
  }
  return undefined;
}

function compareSortValues(a: SortValue, b: SortValue): number {
  const aEmpty = a === null || a === undefined || a === "";
  const bEmpty = b === null || b === undefined || b === "";
  if (aEmpty && bEmpty) return 0;
  if (aEmpty) return 1;
  if (bEmpty) return -1;

  const aNumeric = numericSortValue(a);
  const bNumeric = numericSortValue(b);
  if (aNumeric !== null && bNumeric !== null) {
    return aNumeric - bNumeric;
  }

  return String(a).localeCompare(String(b), undefined, {
    numeric: true,
    sensitivity: "base",
  });
}

function numericSortValue(
  value: Exclude<SortValue, null | undefined>,
): number | null {
  if (typeof value === "number") return value;
  if (typeof value === "boolean") return value ? 1 : 0;
  const text = String(value).trim();
  if (/^-?\d+(?:\.\d+)?%$/.test(text)) return Number(text.slice(0, -1));
  if (/^-?\d+(?:\.\d+)?$/.test(text)) return Number(text);
  const duration = durationSeconds(text);
  return duration;
}

function durationSeconds(text: string): number | null {
  const compact = text.toLowerCase().trim();
  const matches = [...compact.matchAll(/(\d+(?:\.\d+)?)\s*(h|m|s)\b/g)];
  if (matches.length === 0) return null;
  const consumed = matches.map((m) => m[0]).join("").replace(/\s+/g, "");
  if (consumed !== compact.replace(/\s+/g, "")) return null;
  return matches.reduce((total, match) => {
    const value = Number(match[1]);
    const unit = match[2];
    if (unit === "h") return total + value * 3600;
    if (unit === "m") return total + value * 60;
    return total + value;
  }, 0);
}

function textFromChildren(value: ComponentChildren): string {
  if (value === null || value === undefined || typeof value === "boolean") return "";
  if (typeof value === "string" || typeof value === "number") return String(value);
  if (Array.isArray(value)) return value.map(textFromChildren).join(" ");
  if (typeof value === "object" && "props" in value) {
    const props = (value as { props?: { children?: ComponentChildren } }).props;
    return textFromChildren(props?.children);
  }
  return "";
}

function isInteractiveTarget(target: EventTarget | null): boolean {
  if (!(target instanceof Element)) return false;
  return Boolean(
    target.closest(
      "a,button,input,select,textarea,label,summary,[role='button'],[role='link']",
    ),
  );
}
