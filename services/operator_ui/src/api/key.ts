/**
 * API-key bootstrap.
 *
 * FastAPI renders index.html at request time with
 *   <meta name="operator-api-key" content="<DASHBOARD_API_KEY>">
 * — the SPA reads it once on startup. Subsequent fetch() calls attach the
 * key as X-API-Key; SSE (EventSource, which can't send headers) attaches
 * ?api_key= query param.
 *
 * When the key is empty the dashboard renders without auth — matches L1's
 * default-open posture on localhost.
 */

let cached: string | null = null;

function readMeta(): string {
  if (cached !== null) return cached;
  const el = document.querySelector<HTMLMetaElement>('meta[name="operator-api-key"]');
  cached = (el?.content ?? "").trim();
  return cached;
}

export function apiKey(): string {
  return readMeta();
}

export function fetchHeaders(extra: HeadersInit = {}): HeadersInit {
  const key = apiKey();
  return key ? { ...extra, "X-API-Key": key } : extra;
}

export function sseUrl(path: string): string {
  const key = apiKey();
  if (!key) return path;
  const sep = path.includes("?") ? "&" : "?";
  return `${path}${sep}api_key=${encodeURIComponent(key)}`;
}
