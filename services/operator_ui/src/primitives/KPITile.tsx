import { Sparkline } from "./Sparkline";

interface KPITileProps {
  label: string;
  /** Big serif number. */
  value: string;
  /** Optional mono unit/delta suffix, rendered in the accent colour. */
  suffix?: string;
  /** One-line sans subtitle under the number. */
  sub?: string;
  /** Optional sparkline series. */
  trend?: readonly (number | null)[];
  /** Colour class applied to the sparkline (use a signal colour hex inline). */
  trendColor?: string;
}

/**
 * Stacked metric tile: mono uppercase label → big serif number →
 * optional sparkline → optional subtitle.
 *
 * The sparkline inherits ``currentColor``, so set it via ``trendColor``
 * (e.g., ``var(--signal-ok)``).
 */
export function KPITile({
  label,
  value,
  suffix,
  sub,
  trend,
  trendColor,
}: KPITileProps) {
  return (
    <div class="op-kpi">
      <span class="op-kpi-label">{label}</span>
      <span class="op-kpi-val">
        {value}
        {suffix && <em>{suffix}</em>}
      </span>
      {trend && trend.length > 0 && (
        <span
          class="op-kpi-spark"
          style={trendColor ? { color: trendColor } : undefined}
        >
          <Sparkline values={trend} />
        </span>
      )}
      {sub && <span class="op-kpi-sub">{sub}</span>}
    </div>
  );
}
