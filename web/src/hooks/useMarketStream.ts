import { useEffect, useRef, useState } from "react";
import type { Book, Ticker } from "../types";

interface Stream {
  ticker: Ticker | null;
  book: Book;
  connected: boolean;
}

const EMPTY_BOOK: Book = { bids: [], asks: [] };

/**
 * One WebSocket per (market, symbol) pair. The backend refcounts watchers, so
 * switching symbols closes the previous subscription instead of leaving it
 * running. A dropped socket reconnects on its own; the panel stays usable.
 */
export function useMarketStream(market: string, symbol: string): Stream {
  const [ticker, setTicker] = useState<Ticker | null>(null);
  const [book, setBook] = useState<Book>(EMPTY_BOOK);
  const [connected, setConnected] = useState(false);
  const retry = useRef<number | undefined>(undefined);

  useEffect(() => {
    if (!symbol) return;
    setTicker(null);
    setBook(EMPTY_BOOK);

    let socket: WebSocket | null = null;
    let disposed = false;

    const connect = () => {
      const scheme = window.location.protocol === "https:" ? "wss" : "ws";
      socket = new WebSocket(`${scheme}://${window.location.host}/ws/market`);

      socket.onopen = () => {
        if (disposed) {
          socket?.close();
          return;
        }
        socket?.send(JSON.stringify({ action: "subscribe", market, symbol }));
        setConnected(true);
      };

      socket.onmessage = (event) => {
        let frame: unknown;
        try {
          frame = JSON.parse(event.data as string);
        } catch {
          return;
        }
        if (!frame || typeof frame !== "object") return;
        const message = frame as Record<string, unknown>;
        if (message.type === "ticker" && message.symbol === symbol) {
          setTicker(message as unknown as Ticker);
        } else if (message.type === "book" && message.symbol === symbol) {
          setBook({
            bids: (message.bids as [number, number][]) ?? [],
            asks: (message.asks as [number, number][]) ?? [],
          });
        }
      };

      socket.onclose = () => {
        setConnected(false);
        if (!disposed) retry.current = window.setTimeout(connect, 1500);
      };

      socket.onerror = () => socket?.close();
    };

    connect();
    return () => {
      disposed = true;
      window.clearTimeout(retry.current);
      socket?.close();
    };
  }, [market, symbol]);

  return { ticker, book, connected };
}
