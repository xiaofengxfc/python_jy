import { api, query } from "./api";
import type { TickerQuote } from "./types";

/** 24 小时涨幅怎么显示：+1.23% / -0.45%；null 或读不到就留空。
 *  三处（选择器行、收藏与最近用过、交易页标题）都走这两个函数，格式只有一个来源。 */
export function fmtChangePct(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "";
  return `${value > 0 ? "+" : ""}${value.toFixed(2)}%`;
}

/** 涨用 up、跌用 down；null 和 0 都不上色。 */
export function changeTone(value: number | null | undefined): "" | "up" | "down" {
  if (value == null || !Number.isFinite(value) || value === 0) return "";
  return value > 0 ? "up" : "down";
}

/** 一次最多问 50 个：后端 /api/tickers 超过 50 就 400，整批涨跌幅全没。
 *  当前交易对、收藏、最近用过排在前面，它们在窗口里；列表里没问到的行留空。 */
const MAX_SYMBOLS = 50;

/** GET /api/tickers?market=&symbols=a,b,c → { symbol: quote }。
 *  接口没上、网络断了、返回形状不对，都返回空表：拿不到就不显示涨跌幅，不报错、不拿 0 顶。 */
export async function loadTickers(
  market: string,
  symbols: string[],
): Promise<Record<string, TickerQuote>> {
  const wanted = Array.from(new Set(symbols.filter(Boolean))).slice(0, MAX_SYMBOLS);
  if (!wanted.length) return {};
  try {
    const data = await api<{ items?: TickerQuote[] }>(
      `/api/tickers?${query({ market, symbols: wanted.join(",") })}`,
    );
    const out: Record<string, TickerQuote> = {};
    for (const item of data?.items ?? []) {
      if (item && typeof item.symbol === "string" && item.symbol) out[item.symbol] = item;
    }
    return out;
  } catch {
    return {};
  }
}
