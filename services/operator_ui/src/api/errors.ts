export function parseJsonObject(text: string): Record<string, unknown> | null {
  try {
    const value = JSON.parse(text);
    return value && typeof value === "object" && !Array.isArray(value)
      ? (value as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

export function readableError(
  body: Record<string, unknown> | null,
  fallback: string,
): string {
  const raw =
    body?.["error"] ??
    body?.["detail"] ??
    body?.["status_reason"] ??
    fallback;
  return String(raw || "request failed").slice(0, 300);
}

export function readableErrorText(
  text: string,
  fallback = "request failed",
): string {
  return readableError(parseJsonObject(text), text || fallback);
}
