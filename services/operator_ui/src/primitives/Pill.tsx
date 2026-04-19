import type { ComponentChildren } from "preact";

export type PillTone = "active" | "ok" | "warn" | "err" | "cool";

interface PillProps {
  tone: PillTone;
  children: ComponentChildren;
}

/**
 * Status pill — coloured dot + mono uppercase label.
 *
 * Exactly one thing should ever be `"active"` in a given view. The active
 * variant pulses the dot to reinforce that live state.
 */
export function Pill({ tone, children }: PillProps) {
  return (
    <span class={`op-pill is-${tone}`}>
      <span class="op-pill-dot" aria-hidden="true"></span>
      {children}
    </span>
  );
}
