/** SSE client with Last-Event-ID resume and bounded reconnect backoff. */
export interface ServerEvent<T = unknown> { id?: string; event: string; data: T; }
export interface EventStreamOptions<T> { onEvent: (event: ServerEvent<T>) => void; onError?: (error: Error) => void; signal?: AbortSignal; }

const apiBase = (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "") ?? "";
export function eventStreamUrl(path: string, lastEventId: string, origin = window.location.origin) {
  const url = new URL(`${apiBase}${path}`, origin);
  if (lastEventId) url.searchParams.set("last_event_id", lastEventId);
  return url.toString();
}
export function openEventStream<T = unknown>(path: string, options: EventStreamOptions<T>): () => void {
  let stopped = false; let lastId = sessionStorage.getItem(`maf.sse.${path}`) ?? ""; let retry = 0; let source: EventSource | undefined; let reconnectTimer: number | undefined;
  const connect = () => {
    if (stopped) return;
    const url = new URL(eventStreamUrl(path, lastId));
    source = new EventSource(url, { withCredentials: true });
    source.onopen = () => { retry = 0; };
    source.onmessage = (message) => { lastId = message.lastEventId || lastId; if (lastId) sessionStorage.setItem(`maf.sse.${path}`, lastId); let data: T; try { data = JSON.parse(message.data) as T; } catch { data = message.data as T; } options.onEvent({ id: message.lastEventId || undefined, event: "message", data }); };
    source.onerror = () => { source?.close(); if (stopped) return; options.onError?.(new Error("事件连接已断开，正在重试")); const delay = Math.min(30_000, 500 * 2 ** retry++); reconnectTimer = window.setTimeout(connect, delay); };
  };
  const stop = () => { stopped = true; source?.close(); if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer); options.signal?.removeEventListener("abort", stop); };
  options.signal?.addEventListener("abort", stop, { once: true }); connect(); return stop;
}
