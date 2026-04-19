import type { ComponentChildren } from "preact";

interface SectionHeaderProps {
  label: string;
  /** Optional right-aligned content — typically a meta chip or link. */
  right?: ComponentChildren;
}

/**
 * Section header: mono uppercase label with an optional right-rail and
 * a bottom rule. Pair with a 44px vertical gap between sections.
 */
export function SectionHeader({ label, right }: SectionHeaderProps) {
  return (
    <div class="op-sec-hd">
      <span class="op-sec-label">{label}</span>
      {right && <span class="op-sec-right">{right}</span>}
    </div>
  );
}
