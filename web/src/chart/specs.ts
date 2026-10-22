/** 指标定义：名称、参数、取值范围。
 *
 * 两处共用这份定义：TradingPage 的参数编辑器，和图表组件。
 * 放进组件里会循环引用。
 */

export type Indicator =
  | "ma"
  | "ema"
  | "boll"
  | "macd"
  | "rsi"
  | "kdj"
  | "wr"
  | "cci"
  | "obv"
  | "atr"
  | "dmi";

export interface IndicatorParam {
  /** Stable key used in the stored config. */
  key: string;
  label: string;
  min: number;
  max: number;
  step: number;
  /** Value used the first time the indicator is switched on. */
  value: number;
}

export interface IndicatorSpec {
  label: string;
  kind: "main" | "sub";
  params: IndicatorParam[];
}

export type IndicatorParams = Record<string, number>;

/** One switched-on indicator with the user's parameters. */
export interface IndicatorConfig {
  name: Indicator;
  params: IndicatorParams;
}

export const INDICATOR_SPECS: Record<Indicator, IndicatorSpec> = {
  ma: {
    label: "MA",
    kind: "main",
    params: [
      { key: "fast", label: "快", min: 1, max: 400, step: 1, value: 7 },
      { key: "mid", label: "中", min: 1, max: 400, step: 1, value: 25 },
      { key: "slow", label: "慢", min: 1, max: 400, step: 1, value: 60 },
    ],
  },
  ema: {
    label: "EMA",
    kind: "main",
    params: [
      { key: "fast", label: "快", min: 1, max: 400, step: 1, value: 12 },
      { key: "slow", label: "慢", min: 1, max: 400, step: 1, value: 26 },
    ],
  },
  boll: {
    label: "BOLL",
    kind: "main",
    params: [
      { key: "period", label: "周期", min: 2, max: 400, step: 1, value: 20 },
      { key: "mult", label: "倍数", min: 0.5, max: 5, step: 0.1, value: 2 },
    ],
  },
  macd: {
    label: "MACD",
    kind: "sub",
    params: [
      { key: "fast", label: "快", min: 1, max: 200, step: 1, value: 12 },
      { key: "slow", label: "慢", min: 2, max: 400, step: 1, value: 26 },
      { key: "signal", label: "信号", min: 1, max: 100, step: 1, value: 9 },
    ],
  },
  rsi: {
    label: "RSI",
    kind: "sub",
    params: [{ key: "period", label: "周期", min: 2, max: 200, step: 1, value: 14 }],
  },
  kdj: {
    label: "KDJ",
    kind: "sub",
    params: [
      { key: "period", label: "周期", min: 2, max: 200, step: 1, value: 9 },
      { key: "smooth", label: "平滑", min: 1, max: 20, step: 1, value: 3 },
    ],
  },
  wr: {
    label: "WR",
    kind: "sub",
    params: [{ key: "period", label: "周期", min: 2, max: 200, step: 1, value: 14 }],
  },
  cci: {
    label: "CCI",
    kind: "sub",
    params: [{ key: "period", label: "周期", min: 2, max: 200, step: 1, value: 20 }],
  },
  obv: {
    label: "OBV",
    kind: "sub",
    params: [{ key: "period", label: "均线", min: 2, max: 200, step: 1, value: 30 }],
  },
  atr: {
    label: "ATR",
    kind: "sub",
    params: [{ key: "period", label: "周期", min: 2, max: 200, step: 1, value: 14 }],
  },
  dmi: {
    label: "DMI",
    kind: "sub",
    params: [
      { key: "period", label: "周期", min: 2, max: 200, step: 1, value: 14 },
      { key: "adx", label: "ADX", min: 2, max: 200, step: 1, value: 14 },
    ],
  },
};

export const MAIN_INDICATORS: Indicator[] = ["ma", "ema", "boll"];
export const SUB_INDICATORS: Indicator[] = [
  "macd",
  "rsi",
  "kdj",
  "wr",
  "cci",
  "obv",
  "atr",
  "dmi",
];

/** Default parameters for one indicator, as a fresh object. */
export function defaultParams(name: Indicator): IndicatorParams {
  const out: IndicatorParams = {};
  INDICATOR_SPECS[name].params.forEach((param) => {
    out[param.key] = param.value;
  });
  return out;
}

/** Clamp a user-typed value into the parameter's allowed range. */
export function clampParam(name: Indicator, key: string, value: number): number {
  const spec = INDICATOR_SPECS[name].params.find((param) => param.key === key);
  if (!spec || !Number.isFinite(value)) return spec ? spec.value : value;
  return Math.min(spec.max, Math.max(spec.min, value));
}

/** One-line description of a switched-on indicator, parameters included. */
export function legendText(config: IndicatorConfig): string {
  const values = INDICATOR_SPECS[config.name].params.map(
    (param) => config.params[param.key] ?? param.value,
  );
  return INDICATOR_SPECS[config.name].label + "(" + values.join(",") + ")";
}
