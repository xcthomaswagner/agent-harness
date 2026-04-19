export type PhaseState = "done" | "active" | "pending" | "fail";

interface PhaseDotsProps {
  /** Usually 5: planning → scaffolding → implementing → reviewing → merging. */
  phases: readonly PhaseState[];
  /** Disable the shimmer sweep on the active dot. */
  shimmer?: boolean;
}

/**
 * Row of short horizontal bars — one per pipeline phase.
 *
 * Visual language:
 *   done    → filled sage
 *   active  → filled amber with a 3px outer glow halo + shimmer sweep
 *   pending → hollow ink-300
 *   fail    → filled clay
 */
export function PhaseDots({ phases, shimmer = true }: PhaseDotsProps) {
  return (
    <div
      class="op-phase-dots"
      data-shimmer={shimmer ? "on" : "off"}
      role="img"
      aria-label={`Phases: ${phases.join(", ")}`}
    >
      {phases.map((state, i) => (
        <span key={i} class={`op-phase-dot is-${state}`} />
      ))}
    </div>
  );
}
