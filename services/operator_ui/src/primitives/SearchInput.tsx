import { useEffect, useRef } from "preact/hooks";

interface SearchInputProps {
  value: string;
  onInput: (next: string) => void;
  placeholder?: string;
  /** Keyboard hotkey label shown at the trailing edge (e.g., "/"). */
  hotkey?: string;
  /** When true, the global document-level hotkey focuses this input. */
  focusOnHotkey?: boolean;
}

/**
 * Rule-bordered search input with leading glyph and trailing hotkey badge.
 *
 * The hotkey wiring is opt-in via ``focusOnHotkey`` so multiple search
 * inputs on a page don't fight for the same key. Matches the design's
 * "/" pattern from the Tickets view.
 */
export function SearchInput({
  value,
  onInput,
  placeholder = "Search…",
  hotkey = "/",
  focusOnHotkey = false,
}: SearchInputProps) {
  const ref = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (!focusOnHotkey) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key !== hotkey) return;
      const active = document.activeElement;
      if (
        active instanceof HTMLInputElement ||
        active instanceof HTMLTextAreaElement
      ) {
        return;
      }
      e.preventDefault();
      ref.current?.focus();
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [hotkey, focusOnHotkey]);

  return (
    <label class="op-search">
      <span class="op-search-icon" aria-hidden="true">⌕</span>
      <input
        ref={ref}
        type="search"
        value={value}
        placeholder={placeholder}
        onInput={(e) => onInput((e.target as HTMLInputElement).value)}
        spellcheck={false}
      />
      <span class="op-search-hotkey" aria-hidden="true">{hotkey}</span>
    </label>
  );
}
