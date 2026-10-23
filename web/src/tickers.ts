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

/** 后端 /api/tickers 一次只收 50 个（超了 400），列表 300 行就得分成 6 批。 */
const MAX_SYMBOLS = 50;
const MAX_BATCHES = 6;

/** 结果按市场缓存 10 秒：面板每 2 秒轮询一次，6 个请求不能每次都重发。
 *  缓存只存「问到的那一批交易对」；过期整批重来——行情是快照，半新半旧比留空更糟。 */
const CACHE_TTL_MS = 10_000;

interface TickerCacheEntry {
  at: number;
  quotes: Record<string, TickerQuote>;
}

const cache = new Map<string, TickerCacheEntry>();
/** 同一市场同时只跑一个批次序列：撞上的调用方共用这一份，不另起 6 个请求。
 *  序列被中止（组件卸载）之后不再复用，下一次调用重新开一个。 */
const inflight = new Map<string, { task: Promise<Record<string, TickerQuote>>; cancelled: () => boolean }>();

export interface LoadTickerOptions {
  /** 组件卸载 / 面板关掉时中止后面的批次，别让请求在后台继续跑。 */
  signal?: AbortSignal;
}

/** GET /api/tickers?market=&symbols=a,b,c → { symbol: quote }。
 *
 *  接口一次只收 50 个，所以按 50 一批串行问、最多 6 批（覆盖列表的 300 行）。
 *  顺序沿用调用方给的：当前交易对、收藏、最近用过在最前面，第一屏一定有数。
 *  某一批失败只让那一批的行留空，已经拿到的照旧返回；
 *  拿不到就留空，不报错、不拿 0 顶。 */
export async function loadTickers(
  market: string,
  symbols: string[],
  options: LoadTickerOptions = {},
): Promise<Record<string, TickerQuote>> {
  const usable = symbols.filter((item) => symbolFitsMarket(market, item));
  const wanted = Array.from(new Set(usable)).slice(0, MAX_SYMBOLS * MAX_BATCHES);
  if (!wanted.length) return {};
  const cached = cache.get(market);
  if (cached && Date.now() - cached.at < CACHE_TTL_MS) return { ...cached.quotes };
  const running = inflight.get(market);
  if (running && !running.cancelled()) return running.task;
  const signal = options.signal;
  const task = fetchBatches(market, wanted, signal);
  const entry = { task, cancelled: () => Boolean(signal?.aborted) };
  inflight.set(market, entry);
  try {
    return await task;
  } finally {
    // 只清自己那一条：中途被新一轮顶掉时别把新的删了。
    if (inflight.get(market) === entry) inflight.delete(market);
  }
}

async function fetchBatches(
  market: string,
  wanted: string[],
  signal?: AbortSignal,
): Promise<Record<string, TickerQuote>> {
  const merged: Record<string, TickerQuote> = {};
  const batches = Math.min(MAX_BATCHES, Math.ceil(wanted.length / MAX_SYMBOLS));
  let failures = 0;
  for (let index = 0; index < batches; index += 1) {
    // 面板关掉、页面切走之后别再发后面的批次。
    if (signal?.aborted) break;
    const chunk = wanted.slice(index * MAX_SYMBOLS, (index + 1) * MAX_SYMBOLS);
    try {
      const data = await api<{ items?: TickerQuote[] }>(
        `/api/tickers?${query({ market, symbols: chunk.join(",") })}`,
        signal ? { signal } : undefined,
      );
      for (const item of data?.items ?? []) {
        if (item && typeof item.symbol === "string" && item.symbol) merged[item.symbol] = item;
      }
    } catch (caught) {
      // 被中止不是失败：调用方已经不要这批了，别再往 console 里刷一条。
      if (signal?.aborted) break;
      // 只让这一批的行留空：前面批次拿到的结果不清空，清空等于整列白掉。
      failures += 1;
      console.warn(
        `[tickers] ${market} 第 ${index + 1}/${batches} 批失败（${chunk.length} 个交易对）：`,
        caught instanceof Error ? caught.message : String(caught),
      );
    }
  }
  // 被中止的是半份快照，没失败才写缓存；全批失败也不写，让下一次还能重试。
  if (!signal?.aborted && (failures < batches || Object.keys(merged).length > 0)) {
    cache.set(market, { at: Date.now(), quotes: merged });
  }
  return merged;
}
