/** 现货行的浮动盈亏是按账本数量算的：钱包余额里混着用户自己的币，多出来的那部分
 *  不是本程序买的，不该摊进盈亏。两个数不一样时补一句「账本口径 0.0041」，
 *  像是算错了。数量一致（合约行、纯账本行）时返回空串，界面上什么都不加。 */
export function ledgerBasisNote(position: {
  contracts: number;
  unrealized_basis?: "" | "ledger";
  ledger_size?: number;
}): string {
  if (position.unrealized_basis !== "ledger") return "";
  const booked = Math.abs(position.ledger_size ?? 0);
  if (booked <= 0) return "";
  if (Math.abs(booked - Math.abs(position.contracts)) < 1e-9) return "";
  return `账本口径 ${fmt(booked, 6)}`;
}

export function fmt(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return value.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

/** Prices need more decimals as they get smaller; 0.00001234 must not read as 0.00. */
export function fmtPrice(value: number | null | undefined): string {
  const n = Math.abs(Number(value) || 0);
  if (n >= 1000) return fmt(value, 2);
  if (n >= 1) return fmt(value, 2);
  if (n >= 0.01) return fmt(value, 4);
  return fmt(value, 6);
}

export function fmtCompact(value: number | null | undefined): string {
  const n = Number(value) || 0;
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(2)}K`;
  return n.toFixed(2);
}

/** Axis label for a bar. Long bars print the date, intraday bars the clock,
 * and a weekly bar also gets the year so January is never ambiguous. */
export function shortTime(ms: number, timeframe = "15m"): string {
  const date = new Date(ms);
  const mm = String(date.getMonth() + 1).padStart(2, "0");
  const dd = String(date.getDate()).padStart(2, "0");
  const hh = String(date.getHours()).padStart(2, "0");
  const mi = String(date.getMinutes()).padStart(2, "0");
  if (timeframe === "1w") {
    return `${String(date.getFullYear()).slice(2)}/${mm}/${dd}`;
  }  if (timeframe === "1d" || timeframe === "3d") return `${mm}/${dd}`;
  return `${hh}:${mi}`;
}

export function parseNumber(text: string): number {
  const value = Number.parseFloat(text.replace(/,/g, "").trim());
  return Number.isFinite(value) ? value : 0;
}

export const TIMEFRAMES: [string, string][] = [
  ["1m", "1分"],
  ["5m", "5分"],
  ["15m", "15分"],
  ["30m", "30分"],
  ["1h", "1时"],
  ["4h", "4时"],
  ["1d", "1日"],
  ["1w", "1周"],
];

/** Bar duration per timeframe, used to fold live ticks into the last candle. */
export const TIMEFRAME_MS: Record<string, number> = {
  "1m": 60_000,
  "3m": 180_000,
  "5m": 300_000,
  "15m": 900_000,
  "30m": 1_800_000,
  "1h": 3_600_000,
  "2h": 7_200_000,
  "4h": 14_400_000,
  "6h": 21_600_000,
  "8h": 28_800_000,
  "12h": 43_200_000,
  "1d": 86_400_000,
  "3d": 259_200_000,
  "1w": 604_800_000,
};

export const MARKET_LABELS: Record<string, string> = {
  spot: "现货",
  usdm: "U 本位",
  coinm: "币本位",
};
