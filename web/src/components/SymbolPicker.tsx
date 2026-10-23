import { useEffect, useMemo, useRef, useState } from "react";
import { changeTone, fmtChangePct } from "../tickers";
import type { SymbolInfo, TickerQuote } from "../types";

interface Props {
  symbols: SymbolInfo[];
  value: string;
  onChange: (symbol: string) => void;
  loading: boolean;
  /** 收藏的交易对，来自 settings.favorites（跟账户走，不在浏览器里存一份）。 */
  favorites?: string[];
  /** 最近用过的，最多显示 8 条。 */
  recent?: string[];
  /** 这批交易对的 24 小时行情；拿不到就是空对象，涨跌幅那一列整列不显示。 */
  quotes?: Record<string, TickerQuote>;
  /** 点星标加/取消收藏。 */
  onToggleFavorite?: (symbol: string) => void;
  /** 面板开着才有人在看这些涨幅：页面据此决定要不要定时刷新、关掉时中止在跑的请求。 */
  onOpenChange?: (open: boolean) => void;
}

const RECENT_LIMIT = 8;

export default function SymbolPicker({
  symbols,
  value,
  onChange,
  loading,
  favorites = [],
  recent = [],
  quotes = {},
  onToggleFavorite,
  onOpenChange,
}: Props) {
  const [open, setOpen] = useState(false);
  const [term, setTerm] = useState("");
  const boxRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  const current = symbols.find((item) => item.symbol === value);

  useEffect(() => {
    onOpenChange?.(open);
  }, [open, onOpenChange]);

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

  const known = useMemo(() => new Set(symbols.map((item) => item.symbol)), [symbols]);
  const favoritePairs = useMemo(
    () => favorites.filter((pair) => known.has(pair)),
    [favorites, known],
  );
  /** 已经收藏的不再在「最近用过」里重复一遍。 */
  const recentPairs = useMemo(
    () =>
      recent
        .filter((pair) => known.has(pair) && !favorites.includes(pair))
        .slice(0, RECENT_LIMIT),
    [recent, known, favorites],
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

  /** 涨跌幅：拿不到（null / 没问过这个对）就留白，不显示 0。 */
  const pct = (symbol: string) => {
    const text = fmtChangePct(quotes[symbol]?.change_pct ?? null);
    if (!text) return <span className="picker-pct faint" />;
    return <span className={`picker-pct ${changeTone(quotes[symbol]?.change_pct ?? null)}`}>{text}</span>;
  };

  const favorited = (symbol: string) => favorites.includes(symbol);

  const star = (symbol: string) => (
    <button
      type="button"
      className={`picker-star${favorited(symbol) ? " on" : ""}`}
      aria-pressed={favorited(symbol)}
      aria-label={`${favorited(symbol) ? "取消收藏" : "收藏"} ${symbol}`}
      title={favorited(symbol) ? "取消收藏" : "收藏"}
      onClick={(event) => {
        event.stopPropagation();
        onToggleFavorite?.(symbol);
      }}
    >
      {favorited(symbol) ? "★" : "☆"}
    </button>
  );

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

          {!term && (
            <>
              <div className="picker-section">收藏</div>
              {favoritePairs.length > 0 ? (
                <div className="picker-chips">
                  {favoritePairs.map((pair) => (
                    <button key={pair} type="button" onClick={() => pick(pair)}>
                      <span>{pair}</span>
                      {pct(pair)}
                    </button>
                  ))}
                </div>
              ) : (
                <div className="picker-empty">还没有收藏，点右侧星标添加</div>
              )}

              {recentPairs.length > 0 && (
                <>
                  <div className="picker-section">最近用过</div>
                  <div className="picker-chips">
                    {recentPairs.map((pair) => (
                      <button key={pair} type="button" onClick={() => pick(pair)}>
                        <span>{pair}</span>
                        {pct(pair)}
                      </button>
                    ))}
                  </div>
                </>
              )}
            </>
          )}

          <div className="picker-list" role="listbox" aria-label="交易对">
            {matches.length === 0 && <div className="picker-empty">没有匹配的交易对</div>}
            {matches.map((item) => (
              <div
                key={item.symbol}
                role="option"
                aria-selected={item.symbol === value}
                className="picker-option"
              >
                <button type="button" className="picker-option-main" onClick={() => pick(item.symbol)}>
                  <span>{item.display}</span>
                  <span className="faint">{item.quote}</span>
                </button>
                {pct(item.symbol)}
                {star(item.symbol)}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
