import { useEffect, useMemo, useRef, useState } from "react";
import type { SymbolInfo } from "../types";

interface Props {
  symbols: SymbolInfo[];
  value: string;
  onChange: (symbol: string) => void;
  loading: boolean;
}

/** Pairs that most people trade first. Shown as chips so they are one click
 * away instead of buried in a list of several hundred. */
const FAVORITES = ["BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT"];

export default function SymbolPicker({ symbols, value, onChange, loading }: Props) {
  const [open, setOpen] = useState(false);
  const [term, setTerm] = useState("");
  const boxRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  const current = symbols.find((item) => item.symbol === value);

  useEffect(() => {
    if (!open) return;
    const onDown = (event: MouseEvent) => {
      if (!boxRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    const timer = window.setTimeout(() => inputRef.current?.focus(), 0);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
      window.clearTimeout(timer);
    };
  }, [open]);

  const favorites = useMemo(
    () => FAVORITES.filter((pair) => symbols.some((item) => item.symbol === pair)),
    [symbols],
  );

  const matches = useMemo(() => {
    const needle = term.trim().toUpperCase();
    if (!needle) return symbols.slice(0, 300);
    return symbols
      .filter(
        (item) =>
          item.symbol.toUpperCase().includes(needle) ||
          item.base.toUpperCase().startsWith(needle),
      )
      .slice(0, 300);
  }, [symbols, term]);

  const pick = (symbol: string) => {
    onChange(symbol);
    setOpen(false);
    setTerm("");
  };

  return (
    <div className="picker" ref={boxRef}>
      <button
        type="button"
        className="picker-btn"
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <span className="picker-symbol">{current?.display ?? (loading ? "加载中…" : value || "选择交易对")}</span>
        <svg viewBox="0 0 16 16" width="12" height="12" fill="none" aria-hidden="true">
          <path
            d="m4 6 4 4 4-4"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </button>

      {open && (
        <div className="picker-pop">
          <input
            ref={inputRef}
            className="input picker-search"
            type="text"
            placeholder="搜索 BTC、ETH、SOL…"
            value={term}
            onChange={(event) => setTerm(event.target.value)}
          />

          {favorites.length > 0 && !term && (
            <div className="picker-chips">
              {favorites.map((pair) => (
                <button key={pair} type="button" onClick={() => pick(pair)}>
                  {pair}
                </button>
              ))}
            </div>
          )}

          <div className="picker-list" role="listbox" aria-label="交易对">
            {matches.length === 0 && <div className="picker-empty">没有匹配的交易对</div>}
            {matches.map((item) => (
              <button
                key={item.symbol}
                type="button"
                role="option"
                aria-selected={item.symbol === value}
                className="picker-option"
                onClick={() => pick(item.symbol)}
              >
                <span>{item.display}</span>
                <span className="faint">{item.quote}</span>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
