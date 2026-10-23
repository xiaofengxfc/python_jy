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

/** 交割合约的结算币后面还挂一段到期日，BTC/USDT:USDT-250328。 */
const EXPIRY = /-\d{6}$/;

/** 这个写法属于哪个市场：spot / usdm / coinm。
 *  判定和后端 core.types.market_for_symbol 同一口径：没有冒号是现货，
 *  结算币是 USDT/USDC 的归 U 本位，其余归币本位（到期日先切掉）。 */
export function marketForSymbol(symbol: string): "spot" | "usdm" | "coinm" {
  const text = String(symbol ?? "").trim();
  const settle = text.slice(text.indexOf(":") + 1).replace(EXPIRY, "").toUpperCase();
  if (!settle) return "spot";
  return settle === "USDT" || settle === "USDC" ? "usdm" : "coinm";
}

/** 这个交易对能不能拿去问当前市场的批量涨幅。
 *
 *  收藏与最近用过是跨市场的：现货写法 [BTC/USDT, ETH/USDT] 在 usdm 下也留着。
 *  以前原样拼进 /api/tickers，后端按市场收死写法，整批 400「「BTC/USDT」是现货写法」，
 *  300 行涨幅、收藏、最近用过一起空掉。所以拼请求前先按写法筛一遍：不一致的照常
 *  显示在列表里（它们是别的市场的收藏），只是不参与这一次批量请求。
 *  两个以上冒号的写法（BTC/USDT:USDT:USDT）谁都不算，前端先挡一道。 */
export function symbolFitsMarket(market: string, symbol: string): boolean {
  const text = String(symbol ?? "").trim();
  if (!text) return false;
  const colons = (text.match(/:/g) ?? []).length;
  if (colons > 1) return false;
  if (!colons) return market === "spot";
  return market !== "spot" && marketForSymbol(text) === market;
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
  const usable = symbols.filter((item) => symbolFitsMarket(market, item));
  const wanted = Array.from(new Set(usable)).slice(0, MAX_SYMBOLS);
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
  } catch (caught) {
    // 整批失败（多半是写法与市场对不上、或者一次问了太多）：名字照常显示，
    // 涨幅那格留空。原因留一条到 console，别让「整列空白」没法查。
    console.warn(
      `[tickers] ${market} 的批量涨幅读不到（${wanted.length} 个交易对）：`,
      caught instanceof Error ? caught.message : caught,
    );
    return {};
  }
}
