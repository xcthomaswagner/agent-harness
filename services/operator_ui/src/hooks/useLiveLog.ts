/**
 * SSE hook for the per-ticket live log at /api/traces/{id}/stream.
 *
 * The backend replays the last ~N events, then tails session-stream.jsonl
 * with periodic heartbeats. EventSource reconnects automatically on
 * transport errors; this hook tracks (connected | connecting | error)
 * for the UI state badge.
 *
 * Events shape:
 *   {
 *     kind: "tool_use" | "text" | "system" | ...,
 *     teammate, timestamp,
 *     tool_name? | text? | ...,
 *   }
 */

import { useEffect, useRef, useState } from "preact/hooks";
import { sseUrl } from "../api/key";

export type LiveConnState = "idle" | "connecting" | "connected" | "error";

export interface LiveLogEntry {
  event_id?: string;
  kind: string;
  teammate: string;
  role?: string;
  role_group?: "team_lead" | "dev" | "review" | "qa" | "other";
  display_name?: string;
  timestamp: string;
  observed_at?: string;
  tool_name?: string;
  text?: string;
  description?: string;
  source_line?: number | null;
  [key: string]: unknown;
}

const MAX_BUFFERED = 200;

export function useLiveLog(ticketId: string | null): {
  state: LiveConnState;
  entries: LiveLogEntry[];
  error: string | null;
} {
  const [entries, setEntries] = useState<LiveLogEntry[]>([]);
  const [state, setState] = useState<LiveConnState>("idle");
  const [error, setError] = useState<string | null>(null);
  const srcRef = useRef<EventSource | null>(null);

  useEffect(() => {
    srcRef.current?.close();
    srcRef.current = null;
    setEntries([]);
    setError(null);

    if (!ticketId) {
      setState("idle");
      return;
    }

    setState("connecting");
    const url = sseUrl(`/api/traces/${encodeURIComponent(ticketId)}/stream`);
    const es = new EventSource(url);
    srcRef.current = es;

    es.onopen = () => setState("connected");
    es.onerror = () => {
      setState("error");
      setError("SSE disconnected — reconnecting…");
    };
    es.onmessage = (ev: MessageEvent<string>) => {
      try {
        const entry = JSON.parse(ev.data) as LiveLogEntry;
        setEntries((prev) => {
          const next = [entry, ...prev];
          if (next.length > MAX_BUFFERED) next.length = MAX_BUFFERED;
          return next;
        });
      } catch {
        // Ignore malformed event payloads — EventSource reconnects
        // automatically; dropping one bad frame is preferable to
        // exploding the log.
      }
    };

    return () => {
      es.close();
      srcRef.current = null;
    };
  }, [ticketId]);

  return { state, entries, error };
}
