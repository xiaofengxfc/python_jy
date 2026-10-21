import { useEffect, useRef, useState } from "react";

/** Live Freqtrade events through the backend `/ws/ft` proxy.
 *
 * The proxy owns the JWT; the browser only sees status/entry/exit messages.
 * Reconnects with a short backoff while the engine is running. */
export function useFreqtradeStream(
  enabled: boolean,
  onMessage: (message: { type?: string; data?: unknown }) => void,
): boolean {
  const [connected, setConnected] = useState(false);
  const handler = useRef(onMessage);
  handler.current = onMessage;

  useEffect(() => {
    if (!enabled) {
      setConnected(false);
      return;
    }
    let closed = false;
    let socket: WebSocket | null = null;
    let retry: number | undefined;

    const connect = () => {
      if (closed) return;
      const scheme = window.location.protocol === "https:" ? "wss" : "ws";
      socket = new WebSocket(`${scheme}://${window.location.host}/ws/ft`);
      socket.onopen = () => setConnected(true);
      socket.onmessage = (event) => {
        try {
          handler.current(JSON.parse(String(event.data)));
        } catch {
          // Ignore malformed messages; Freqtrade may add new event types.
        }
      };
      socket.onclose = () => {
        setConnected(false);
        if (!closed) retry = window.setTimeout(connect, 3000);
      };
      socket.onerror = () => socket?.close();
    };

    connect();
    return () => {
      closed = true;
      if (retry !== undefined) window.clearTimeout(retry);
      socket?.close();
    };
  }, [enabled]);

  return connected;
}
