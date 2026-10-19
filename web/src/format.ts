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

export function shortTime(ms: number, daily = false): string {
  const date = new Date(ms);
  const mm = String(date.getMonth() + 1).padStart(2, "0");
  const dd = String(date.getDate()).padStart(2, "0");
  const hh = String(date.getHours()).padStart(2, "0");
  const mi = String(date.getMinutes()).padStart(2, "0");
  return daily ? `${mm}/${dd}` : `${hh}:${mi}`;
}

export function parseNumber(text: string): number {
  const value = Number.parseFloat(text.replace(/,/g, "").trim());
  return Number.isFinite(value) ? value : 0;
}

export const TIMEFRAMES: [string, string][] = [
  ["1m", "1分"],
  ["5m", "5分"],
  ["15m", "15分"],
  ["1h", "1时"],
  ["4h", "4时"],
  ["1d", "1日"],
];

export const MARKET_LABELS: Record<string, string> = {
  spot: "现货",
  usdm: "U 本位",
  coinm: "币本位",
};
