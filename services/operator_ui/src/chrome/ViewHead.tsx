import type { ComponentChildren } from "preact";

interface ViewHeadProps {
  sup?: string;
  title: string;
  sub?: string;
  /** Big right-rail number + label (optional). */
  rnum?: string;
  rlabel?: string;
  /** Freeform right-side actions (replaces rnum/rlabel when both set). */
  right?: ComponentChildren;
}

/**
 * Shared view head: optional eyebrow, serif display title, subtitle, and
 * either a stat block or freeform action slot on the right.
 */
export function ViewHead({
  sup,
  title,
  sub,
  rnum,
  rlabel,
  right,
}: ViewHeadProps) {
  return (
    <header class="op-view-hd">
      <div class="op-view-hd-left">
        {sup && <span class="op-view-sup">{sup}</span>}
        <h1 class="op-view-title">{title}</h1>
        {sub && <span class="op-view-sub">{sub}</span>}
      </div>
      {right ? (
        <div class="op-view-hd-right">{right}</div>
      ) : (
        rnum && (
          <div class="op-view-hd-right">
            <span class="op-view-hd-rnum">{rnum}</span>
            {rlabel && <span class="op-view-hd-rlbl">{rlabel}</span>}
          </div>
        )
      )}
    </header>
  );
}
