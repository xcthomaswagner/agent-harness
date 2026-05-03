import type { ComponentChildren } from "preact";

interface ChipProps {
  label: ComponentChildren;
  /** Optional count badge trailing the label. */
  count?: number;
  /** Selected/active filter state. */
  on?: boolean;
  onClick?: () => void;
}

/**
 * Mono uppercase filter tag with optional count badge.
 *
 * Use for filter rows (Tickets page status filters, Autonomy profile
 * switcher, Learning state filters). `on` paints the chip with the accent
 * colour to signal the active filter.
 */
export function Chip({ label, count, on = false, onClick }: ChipProps) {
  return (
    <button
      type="button"
      class={`op-chip${on ? " is-on" : ""}`}
      onClick={onClick}
      aria-pressed={on}
    >
      <span>{label}</span>
      {typeof count === "number" && (
        <span class="op-chip-n">{count}</span>
      )}
    </button>
  );
}
