interface SparklineProps {
  /** Series of values, left-to-right. Null entries = missing data, skipped. */
  values: readonly (number | null)[];
  width?: number;
  height?: number;
  strokeWidth?: number;
  /** Optional CSS class so consumers can style the stroke colour. */
  className?: string;
}

/**
 * Inline polyline sparkline. ``currentColor`` so the stroke inherits from
 * the tile's signal colour (e.g., sage for FPA, clay for escape).
 *
 * Missing values break the line into segments so an incomplete week doesn't
 * render a misleading trend.
 */
export function Sparkline({
  values,
  width = 120,
  height = 32,
  strokeWidth = 1.5,
  className,
}: SparklineProps) {
  const numeric = values.filter((v): v is number => typeof v === "number");
  if (numeric.length < 2) {
    return (
      <svg
        class={`op-sparkline${className ? ` ${className}` : ""}`}
        width={width}
        height={height}
        aria-hidden="true"
      />
    );
  }

  const min = Math.min(...numeric);
  const max = Math.max(...numeric);
  const range = max - min || 1;

  const x = (i: number) =>
    values.length === 1 ? 0 : (i / (values.length - 1)) * width;
  const y = (v: number) =>
    height - ((v - min) / range) * (height - strokeWidth * 2) - strokeWidth;

  // Break the polyline into segments around null entries.
  const segments: string[] = [];
  let current: string[] = [];
  values.forEach((v, i) => {
    if (v === null) {
      if (current.length) {
        segments.push(current.join(" "));
        current = [];
      }
      return;
    }
    current.push(`${x(i)},${y(v)}`);
  });
  if (current.length) segments.push(current.join(" "));

  return (
    <svg
      class={`op-sparkline${className ? ` ${className}` : ""}`}
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      aria-hidden="true"
    >
      {segments.map((pts, i) => (
        <polyline
          key={i}
          points={pts}
          fill="none"
          stroke="currentColor"
          stroke-width={strokeWidth}
          stroke-linejoin="round"
          stroke-linecap="round"
        />
      ))}
    </svg>
  );
}
