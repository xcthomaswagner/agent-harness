import type { ComponentChildren } from "preact";

interface TableColumn<Row> {
  key: string;
  /** Header label (mono uppercase). */
  label: string;
  /** Cell renderer. */
  render: (row: Row) => ComponentChildren;
  /** Right-align numeric cells. */
  numeric?: boolean;
  /** Fixed width in CSS units. Leave unset for flex. */
  width?: string;
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
  return (
    <table class={`op-tbl${large ? " is-lg" : ""}`}>
      <thead>
        <tr>
          {columns.map((col) => (
            <th
              key={col.key}
              style={col.width ? { width: col.width } : undefined}
              class={col.numeric ? "is-num" : undefined}
            >
              {col.label}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.length === 0 ? (
          <tr>
            <td colSpan={columns.length} style={{ textAlign: "center", padding: "32px 12px", color: "var(--ink-500)" }}>
              {empty ?? "No rows"}
            </td>
          </tr>
        ) : (
          rows.map((row) => (
            <tr
              key={rowKey(row)}
              class={isLive?.(row) ? "is-live" : undefined}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
              style={onRowClick ? { cursor: "pointer" } : undefined}
            >
              {columns.map((col) => (
                <td
                  key={col.key}
                  class={col.numeric ? "is-num" : undefined}
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
