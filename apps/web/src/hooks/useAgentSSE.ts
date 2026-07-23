/**
 * SSE hook for /api/agent/stream — reconnect with Last-Event-ID resume.
 */

import { useCallback, useRef } from "react";

type EventHandler = (data: Record<string, unknown>) => void;
type Handlers = Record<string, EventHandler>;

export type SSEStatus = "disconnected" | "connected" | "reconnecting";

const AGENT_SSE_EVENTS = [
  "turn_started",
  "tool_start",
  "tool_end",
  "section",
  "specialist_started",
  "specialist_done",
  "skill_loaded",
  "status",
  "delta",
  "done",
  "interrupted",
  "error",
  "heartbeat",
] as const;

interface SSEConfig {
  initialRetryMs?: number;
  maxRetryMs?: number;
  backoffFactor?: number;
  dedupeCapacity?: number;
}

const DEFAULTS: Required<SSEConfig> = {
  initialRetryMs: 1000,
  maxRetryMs: 30000,
  backoffFactor: 2,
  dedupeCapacity: 500,
};

export function useAgentSSE(config?: SSEConfig) {
  const opts = { ...DEFAULTS, ...config };
  const sourceRef = useRef<EventSource | null>(null);
  const handlersRef = useRef<Handlers>({});
  const urlRef = useRef("");
  const closedRef = useRef(true);
  const retryCountRef = useRef(0);
  const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastEventIdRef = useRef<string | null>(null);
  const statusRef = useRef<SSEStatus>("disconnected");
  const onStatusChangeRef = useRef<((s: SSEStatus) => void) | null>(null);
  const seenIdsRef = useRef<Set<string>>(new Set());
  const seenOrderRef = useRef<string[]>([]);

  const trackEventId = useCallback(
    (eventId: string): boolean => {
      if (!eventId) return false;
      const seen = seenIdsRef.current;
      const order = seenOrderRef.current;
      if (seen.has(eventId)) return true;
      seen.add(eventId);
      order.push(eventId);
      if (order.length > opts.dedupeCapacity) {
        const oldest = order.shift()!;
        seen.delete(oldest);
      }
      return false;
    },
    [opts.dedupeCapacity],
  );

  const setStatus = useCallback((s: SSEStatus) => {
    statusRef.current = s;
    onStatusChangeRef.current?.(s);
  }, []);

  const buildUrl = useCallback((baseUrl: string) => {
    if (!lastEventIdRef.current) return baseUrl;
    const sep = baseUrl.includes("?") ? "&" : "?";
    return `${baseUrl}${sep}Last-Event-ID=${encodeURIComponent(lastEventIdRef.current)}`;
  }, []);

  const scheduleReconnect = useCallback(() => {
    if (closedRef.current) return;
    retryCountRef.current += 1;
    const delay = Math.min(
      opts.initialRetryMs * Math.pow(opts.backoffFactor, retryCountRef.current - 1),
      opts.maxRetryMs,
    );
    setStatus("reconnecting");
    handlersRef.current.reconnect?.({ attempt: retryCountRef.current, delayMs: delay });
    retryTimerRef.current = setTimeout(() => {
      retryTimerRef.current = null;
      doConnect();
    }, delay);
  }, [opts.initialRetryMs, opts.backoffFactor, opts.maxRetryMs, setStatus]);

  const doConnect = useCallback(() => {
    if (closedRef.current) return;
    const source = new EventSource(buildUrl(urlRef.current));
    sourceRef.current = source;

    source.onopen = () => {
      retryCountRef.current = 0;
      setStatus("connected");
    };

    const handleRaw = (eventType: string, raw: MessageEvent) => {
      if (raw.lastEventId) lastEventIdRef.current = raw.lastEventId;
      if (raw.lastEventId && trackEventId(raw.lastEventId)) return;

      let parsed: Record<string, unknown>;
      try {
        parsed = JSON.parse(raw.data) as Record<string, unknown>;
      } catch {
        parsed = { raw: raw.data };
      }

      const handler = handlersRef.current[eventType] ?? handlersRef.current.message;
      handler?.(parsed);
    };

    for (const eventType of AGENT_SSE_EVENTS) {
      source.addEventListener(eventType, (e) => handleRaw(eventType, e as MessageEvent));
    }

    source.onmessage = (e) => handleRaw("message", e);

    source.onerror = () => {
      if (closedRef.current) return;
      source.close();
      sourceRef.current = null;
      scheduleReconnect();
    };
  }, [buildUrl, trackEventId, setStatus, scheduleReconnect]);

  const connect = useCallback(
    (url: string, handlers: Handlers) => {
      closedRef.current = true;
      sourceRef.current?.close();
      if (retryTimerRef.current) {
        clearTimeout(retryTimerRef.current);
        retryTimerRef.current = null;
      }

      urlRef.current = url;
      handlersRef.current = handlers;
      closedRef.current = false;
      retryCountRef.current = 0;
      lastEventIdRef.current = null;
      seenIdsRef.current.clear();
      seenOrderRef.current.length = 0;

      doConnect();
    },
    [doConnect],
  );

  const disconnect = useCallback(() => {
    closedRef.current = true;
    if (retryTimerRef.current) {
      clearTimeout(retryTimerRef.current);
      retryTimerRef.current = null;
    }
    sourceRef.current?.close();
    sourceRef.current = null;
    setStatus("disconnected");
  }, [setStatus]);

  const onStatusChange = useCallback((cb: (s: SSEStatus) => void) => {
    onStatusChangeRef.current = cb;
  }, []);

  return { connect, disconnect, onStatusChange };
}
