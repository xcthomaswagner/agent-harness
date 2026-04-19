import type { ComponentChildren } from "preact";

type ButtonVariant = "default" | "primary" | "ghost" | "danger";
type ButtonSize = "md" | "sm";

interface ButtonProps {
  children: ComponentChildren;
  variant?: ButtonVariant;
  size?: ButtonSize;
  type?: "button" | "submit" | "reset";
  disabled?: boolean;
  onClick?: (e: Event) => void;
  title?: string;
}

/**
 * Mono uppercase square button. Variants follow the design system:
 *   default  → outlined
 *   primary  → ink-900 fill, flips to accent on hover
 *   ghost    → text only, no rule
 *   danger   → clay rule, inverts on hover
 */
export function Button({
  children,
  variant = "default",
  size = "md",
  type = "button",
  disabled,
  onClick,
  title,
}: ButtonProps) {
  const cls = [
    "op-btn",
    variant !== "default" ? `is-${variant}` : "",
    size === "sm" ? "is-sm" : "",
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <button
      type={type}
      class={cls}
      onClick={onClick}
      disabled={disabled}
      title={title}
    >
      {children}
    </button>
  );
}
