/** Small formatting helpers shared across views. */

/** Render a proportion (0..1 or null) as "NN%" or "—". */
export function pct(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return `${Math.round(value * 100)}%`;
}

/** Render an integer count, falling back to "—" for null/undefined. */
export function intOrDash(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return String(value);
}
