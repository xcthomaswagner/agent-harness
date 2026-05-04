/**
 * Brand glyph: ink-filled square with a 4px amber dot centered. Pair
 * with the wordmark in the Sidebar header.
 */
export function BrandGlyph() {
  return (
    <span class="op-glyph" aria-hidden="true">
      <span class="op-glyph-dot" />
    </span>
  );
}

export function Wordmark() {
  return (
    <span class="op-wordmark" aria-label="Agentic Harness">
      <b>Agentic</b> <span>Harness</span>
    </span>
  );
}
