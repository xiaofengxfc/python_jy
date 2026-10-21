import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { fmt, fmtPrice, parseNumber } from "../format";
import type {
  OrderPreview,
  OrderType,
  PaperProtection,
  Side,
  SizeMode,
} from "../types";

interface Props {
  market: string;
  symbol: string;
  base: string;
  quote: string;
  lastPrice: number;
  cash: number;
  positionQty: number;
  realizedPnl: number;
  /** Configured default order size in quote currency. */
  defaultQuote?: number;
  feeRate: number;
  pickedPrice: number | null;
  protection?: PaperProtection | null;
  /** Paper is the default; live routes the order to the exchange. */
  mode: "paper" | "live";
  onPickedPriceConsumed: () => void;
  onFilled: () => void;
  onResetPaper: () => void | Promise<void>;
}

interface Flash {
  tone: "ok" | "err" | "warn";
  text: string;
}

/** Last size the user actually typed, remembered across symbol switches so
 * the common case (trade 100 USDT of something else) is one click away. */
const LAST_QUOTE_KEY = "bitrader.order.quote";

function readLastQuote(): string {
  try {
    return window.localStorage.getItem(LAST_QUOTE_KEY) ?? "";
  } catch {
    return "";
  }
}

function writeLastQuote(value: string) {
  try {
    if (value) window.localStorage.setItem(LAST_QUOTE_KEY, value);
  } catch {
    /* private mode — falling back to no memory is fine */
  }
}

/** One vertical path: pick a side, set price, set size, read the preview,
 * press the single primary button. Nothing else competes for attention. */
export default function OrderTicket({
  market,
  symbol,
  base,
  quote,
  lastPrice,
  cash,
  positionQty,
  realizedPnl,
  defaultQuote = 0,
  feeRate,
  pickedPrice,
  protection,
  mode,
  onPickedPriceConsumed,
  onFilled,
  onResetPaper,
}: Props) {
  const [side, setSide] = useState<Side>("buy");
  const [orderType, setOrderType] = useState<OrderType>("limit");
  const [sizeMode, setSizeMode] = useState<SizeMode>("quote");
  const [price, setPrice] = useState("");
  const [size, setSize] = useState(() => {
    const remembered = readLastQuote();
    return remembered || (defaultQuote > 0 ? String(defaultQuote) : "");
  });
  const [stopLoss, setStopLoss] = useState("");
  const [takeProfit, setTakeProfit] = useState("");
  const [trigger, setTrigger] = useState("");
  const [triggerDirection, setTriggerDirection] = useState<"below" | "above">("below");
  const [conditionalBusy, setConditionalBusy] = useState(false);
  const [preview, setPreview] = useState<OrderPreview | null>(null);
  const [previewError, setPreviewError] = useState("");
  const [flash, setFlash] = useState<Flash | null>(null);
  const [busy, setBusy] = useState(false);
  /** Second opinion on the pending order. Cache lives on the server, keyed by
   * candle and size bucket, so clicking twice on the same bar is free. */
  const [prejudgeText, setPrejudgeText] = useState("");
  const [prejudgeError, setPrejudgeError] = useState("");
  const [prejudgeBusy, setPrejudgeBusy] = useState(false);
  const requestId = useRef(0);

  // A click on the ladder fills the limit price; the parent clears the signal
  // so the same price can be picked again.
  useEffect(() => {
    if (pickedPrice === null) return;
    setOrderType("limit");
    setPrice(String(pickedPrice));
    onPickedPriceConsumed();
  }, [pickedPrice, onPickedPriceConsumed]);

  // Switching symbol must not carry a stale price or size into the new market.
  // The size falls back to the last amount the user actually typed, because
  // "trade the same 100 USDT of another pair" is the common case.
  const priceSeeded = useRef(false);
  useEffect(() => {
    priceSeeded.current = false;
    setPrice("");
    setSize(
      sizeMode === "quote"
        ? readLastQuote() || (defaultQuote > 0 ? String(defaultQuote) : "")
        : "",
    );
    setStopLoss("");
    setTakeProfit("");
    setPreview(null);
    setPreviewError("");
    setFlash(null);
    // `sizeMode` is read as it stands when the symbol changes; depending on it
    // would wipe the form every time the user flips the unit.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbol, market]);

  // Settings can arrive after the ticket mounted (App loads them async).
  // Fill the size only while the user has not typed anything; never overwrite
  // an amount already being edited.
  useEffect(() => {
    if (sizeMode !== "quote" || defaultQuote <= 0) return;
    setSize((current) =>
      current.trim() ? current : String(defaultQuote),
    );
  }, [defaultQuote, sizeMode]);

  // Flipping side must not leave a limit price from the other direction: a buy
  // parked at 60,000 becomes a market-crossing sell once the user switches, and
  // silently fills far from the quote. Re-seed from the live price instead.
  const sideSeeded = useRef<Side>("buy");
  useEffect(() => {
    if (sideSeeded.current === side) return;
    sideSeeded.current = side;
    setPreview(null);
    setPreviewError("");
    if (lastPrice > 0) setPrice(String(lastPrice));
  }, [side, lastPrice]);

  // Seed the limit price once the new symbol's first quote arrives.
  useEffect(() => {
    if (priceSeeded.current || !(lastPrice > 0)) return;
    setPrice(String(lastPrice));
    priceSeeded.current = true;
  }, [lastPrice]);

  const referencePrice = useMemo(() => {
    const typed = parseNumber(price);
    if (typed > 0) return typed;
    return lastPrice;
  }, [price, lastPrice]);

  // A spot sell is an exit; only a buy (or a futures sell) opens a position
  // and can therefore carry a guard. A held spot position can still be
  // protected — but through the explicit "apply to position" action.
  const opening = side === "buy" || market !== "spot";
  const short = side === "sell" && market !== "spot";
  const hasPosition = Math.abs(positionQty) > 1e-12;
  const positionLong = positionQty > 0;

  const riskyPrices = (forShort: boolean) => {
    const stop = parseNumber(stopLoss);
    const target = parseNumber(takeProfit);
    if (referencePrice <= 0) return "";
    if (forShort) {
      if (stop > 0 && stop <= referencePrice) return "空单止损价必须高于开仓价。";
      if (target > 0 && target >= referencePrice) return "空单止盈价必须低于开仓价。";
      return "";
    }
    if (stop > 0 && stop >= referencePrice) return "止损价必须低于开仓价。";
    if (target > 0 && target <= referencePrice) return "止盈价必须高于开仓价。";
    return "";
  }

  const estimated = useMemo(() => {
    const amount = parseNumber(size);
    if (!amount || !referencePrice) return null;
    return sizeMode === "quote"
      ? { quote: amount, base: amount / referencePrice }
      : { quote: amount * referencePrice, base: amount };
  }, [size, sizeMode, referencePrice]);

  // Debounced dry run: sizing rounding, fees and the risk verdict all come
  // from the same code path a real submission uses.
  useEffect(() => {
    const amount = parseNumber(size);
    if (!symbol || amount <= 0) {
      setPreview(null);
      setPreviewError("");
      return;
    }
    if (orderType === "limit" && referencePrice <= 0) {
      setPreview(null);
      setPreviewError("");
      return;
    }

    const id = ++requestId.current;
    const timer = window.setTimeout(async () => {
      try {
        const result = await api<OrderPreview>("/api/orders/preview", {
          method: "POST",
          body: JSON.stringify({
            market,
            symbol,
            side,
            order_type: orderType,
            size_mode: sizeMode,
            size: amount,
            price: orderType === "limit" ? referencePrice : null,
          }),
        });
        if (id !== requestId.current) return;
        setPreview(result);
        setPreviewError("");
      } catch (error) {
        if (id !== requestId.current) return;
        setPreview(null);
        setPreviewError(error instanceof Error ? error.message : String(error));
      }
    }, 220);

    return () => window.clearTimeout(timer);
  }, [market, symbol, side, orderType, sizeMode, size, referencePrice]);

  /** Ask the assistant whether this order makes sense right now. Deliberately
   * separate from `submit`: it must be possible to ignore it, and it must never
   * be able to block the order. */
  const askPrejudge = async () => {
    if (!preview) return;
    setPrejudgeBusy(true);
    setPrejudgeError("");
    setPrejudgeText("");
    try {
      const result = await api<{ text: string; cached: boolean }>("/api/ai/prejudge", {
        method: "POST",
        body: JSON.stringify({
          market,
          symbol,
          timeframe: "1h",
          side,
          quote: Number(preview.quote_notional ?? 0),
        }),
      });
      setPrejudgeText(result.text + (result.cached ? "\n（同一根 K 线的缓存结论）" : ""));
    } catch (error) {
      setPrejudgeError(
        error instanceof Error
          ? error.message
          : String(error),
      );
    } finally {
      setPrejudgeBusy(false);
    }
  };

  /** Budget the ratio is applied against: cash when buying, the held coin
   * when selling. This is the fix for "100%" meaning different things
   * depending on the side. */
  const available = useMemo(() => {
    if (sizeMode === "quote") {
      return side === "buy" ? cash : positionQty * referencePrice;
    }
    return side === "buy"
      ? referencePrice > 0
        ? cash / referencePrice
        : 0
      : positionQty;
  }, [side, sizeMode, cash, positionQty, referencePrice]);

  const applyRatio = (ratio: number) => {
    const amount = available * ratio;
    if (!(amount > 0)) return;
    setSize(
      sizeMode === "quote"
        ? String(Number(amount.toFixed(2)))
        : String(Number(amount.toFixed(8))),
    );
  };

  const submit = async () => {
    const amount = parseNumber(size);
    if (!symbol) return setFlash({ tone: "err", text: "请先选择交易对。" });
    if (amount <= 0) return setFlash({ tone: "err", text: "请填写数量或金额。" });
    if (orderType === "limit" && referencePrice <= 0) {
      return setFlash({ tone: "err", text: "限价单需要填写价格。" });
    }
    const stop = parseNumber(stopLoss);
    const target = parseNumber(takeProfit);
    if (opening) {
      const problem = riskyPrices(short);
      if (problem) return setFlash({ tone: "err", text: problem });
    }

    setBusy(true);
    setFlash(null);
    try {
      const result = await api<{
        order: Record<string, unknown>;
        warnings?: string[];
      }>("/api/orders", {
        method: "POST",
        body: JSON.stringify({
          market,
          symbol,
          side,
          order_type: orderType,
          size_mode: sizeMode,
          size: amount,
          price: orderType === "limit" ? referencePrice : null,
          stop_loss: opening && stop > 0 ? stop : null,
          take_profit: opening && target > 0 ? target : null,
          mode,
          // The server refuses a live order without this flag; the panel has
          // already asked the user before it gets this far.
          confirm_live: mode === "live",
        }),
      });
      const order = result.order ?? {};
      const filled = Number(order.average ?? order.price ?? referencePrice);
      const guard = [
        opening && stop > 0 ? `止损 ${fmtPrice(stop)}` : "",
        opening && target > 0 ? `止盈 ${fmtPrice(target)}` : "",
      ]
        .filter(Boolean)
        .join(" · ");
      const warnings = result.warnings ?? [];
      const filledText = `${side === "buy" ? "买入" : "卖出"}成功：${fmt(
        Number(order.amount ?? 0),
        6,
      )} ${base} @ ${fmtPrice(filled)}${guard ? `（${guard}）` : ""}`;
      setFlash(
        warnings.length
          ? { tone: "warn", text: `${filledText}。${warnings.join("；")}` }
          : { tone: "ok", text: filledText },
      );
      if (sizeMode === "quote") writeLastQuote(String(amount));
      setSize("");
      setStopLoss("");
      setTakeProfit("");
      setPreview(null);
      onFilled();
    } catch (error) {
      setFlash({ tone: "err", text: error instanceof Error ? error.message : String(error) });
    } finally {
      setBusy(false);
    }
  };

  /** Arm a standalone trigger order: it waits for the market to cross the
   * trigger price and only then becomes a real order. */
  const submitConditional = async () => {
    const amount = parseNumber(size);
    const level = parseNumber(trigger);
    if (amount <= 0) return setFlash({ tone: "err", text: "请填写数量或金额。" });
    if (level <= 0) return setFlash({ tone: "err", text: "请填写触发价。" });
    if (orderType === "limit" && referencePrice <= 0) {
      return setFlash({ tone: "err", text: "限价条件单需要填写委托价。" });
    }
    setConditionalBusy(true);
    setFlash(null);
    try {
      const result = await api<{ conditional: { trigger: number }; amount_base: number }>(
        "/api/orders/conditional",
        {
          method: "POST",
          body: JSON.stringify({
            market,
            symbol,
            side,
            order_type: orderType,
            size_mode: sizeMode,
            size: amount,
            trigger: level,
            direction: triggerDirection,
            price: orderType === "limit" ? referencePrice : null,
            reduce_only: side === "sell" && !opening,
          }),
        },
      );
      setFlash({
        tone: "ok",
        text: `条件单已挂出：${triggerDirection === "below" ? "跌破" : "涨破"} ${fmtPrice(
          result.conditional.trigger,
        )} 后${side === "buy" ? "买入" : "卖出"} ${fmt(result.amount_base, 6)} ${base}`,
      });
      setTrigger("");
      onFilled();
    } catch (error) {
      setFlash({ tone: "err", text: error instanceof Error ? error.message : String(error) });
    } finally {
      setConditionalBusy(false);
    }
  };

  const clearProtection = async () => {
    try {
      await api(`/api/positions/protection?${new URLSearchParams({ symbol })}`, {
        method: "DELETE",
      });
      setFlash({ tone: "ok", text: "已撤销止损/止盈设置。" });
      onFilled();
    } catch (error) {
      setFlash({ tone: "err", text: error instanceof Error ? error.message : String(error) });
    }
  };

  /** Arm or update the guard of the position that is already open. */
  const applyProtection = async () => {
    const stop = parseNumber(stopLoss);
    const target = parseNumber(takeProfit);
    if (!hasPosition) return setFlash({ tone: "err", text: "当前没有持仓。" });
    if (!stop && !target) return setFlash({ tone: "err", text: "请填写止损或止盈价。" });
    const problem = riskyPrices(!positionLong);
    if (problem) return setFlash({ tone: "err", text: problem });
    setBusy(true);
    try {
      await api("/api/positions/protection", {
        method: "PUT",
        body: JSON.stringify({ symbol, stop_loss: stop, take_profit: target }),
      });
      setFlash({
        tone: "ok",
        text: `已应用到持仓：${[
          stop > 0 ? `止损 ${fmtPrice(stop)}` : "",
          target > 0 ? `止盈 ${fmtPrice(target)}` : "",
        ]
          .filter(Boolean)
          .join(" · ")}`,
      });
      onFilled();
    } catch (error) {
      setFlash({ tone: "err", text: error instanceof Error ? error.message : String(error) });
    } finally {
      setBusy(false);
    }
  };

  const blocked = preview ? !preview.allowed : false;
  const sizeLabel = sizeMode === "quote" ? `金额（${quote}）` : `数量（${base}）`;
  const canSubmit = !busy && !blocked && Boolean(size) && Boolean(symbol);

  return (
    <div className="card">
      <div className="card-head">
        <span className="card-title">下单</span>
        <span className="spacer" />
        <div className="segmented">
          <button
            type="button"
            aria-pressed={orderType === "limit"}
            onClick={() => setOrderType("limit")}
          >
            限价
          </button>
          <button
            type="button"
            aria-pressed={orderType === "market"}
            onClick={() => setOrderType("market")}
          >
            市价
          </button>
        </div>
      </div>

      <div className="ticket-body">
        {/* Step 1 — which direction. */}
        <div className="side-switch" role="group" aria-label="买卖方向">
          <button
            type="button"
            data-side="buy"
            aria-pressed={side === "buy"}
            onClick={() => setSide("buy")}
          >
            买入 {base}
          </button>
          <button
            type="button"
            data-side="sell"
            aria-pressed={side === "sell"}
            onClick={() => setSide("sell")}
          >
            卖出 {base}
          </button>
        </div>

        {/* Step 2 — price. */}
        <div className="field">
          <label className="field-label" htmlFor="price-input">
            <span>价格</span>
            <em>{orderType === "market" ? "以最新价成交" : `${quote}`}</em>
          </label>
          <input
            id="price-input"
            className="input"
            type="text"
            inputMode="decimal"
            autoComplete="off"
            disabled={orderType === "market"}
            placeholder={lastPrice ? fmtPrice(lastPrice) : "0.00"}
            value={price}
            onChange={(event) => setPrice(event.target.value)}
          />
        </div>

        {/* Step 3 — size. */}
        <div className="field">
          <label className="field-label" htmlFor="size-input">
            <span>{sizeLabel}</span>
            <em>
              {side === "buy"
                ? `可用 ${fmt(cash, 2)} ${quote}`
                : `持有 ${fmt(positionQty, 6)} ${base}`}
            </em>
          </label>
          <div className="ticket-row">
            <input
              id="size-input"
              className="input"
              type="text"
              inputMode="decimal"
              autoComplete="off"
              placeholder={sizeMode === "quote" ? "0.00" : "0.000000"}
              value={size}
              onChange={(event) => setSize(event.target.value)}
            />
            <div className="segmented">
              <button
                type="button"
                aria-pressed={sizeMode === "quote"}
                onClick={() => setSizeMode("quote")}
              >
                {quote}
              </button>
              <button
                type="button"
                aria-pressed={sizeMode === "amount"}
                onClick={() => setSizeMode("amount")}
              >
                {base}
              </button>
            </div>
          </div>
          <div className="ratio-row">
            {[0.25, 0.5, 0.75, 1].map((ratio) => (
              <button
                key={ratio}
                type="button"
                className="ratio"
                onClick={() => applyRatio(ratio)}
                title={
                  sizeMode === "quote"
                    ? `可用 ${quote} 的 ${ratio * 100}%`
                    : `可卖 ${base} 的 ${ratio * 100}%`
                }
              >
                {ratio === 1 ? "全部" : `${ratio * 100}%`}
              </button>
            ))}
          </div>

          {/* The conversion sits directly under the input, so the number the
              user just typed is answered on the spot instead of three rows
              further down. */}
          <div className="ticket-summary">
            {estimated ? (
              <>
                <span className="num">
                  ≈ {fmt(estimated.base, 6)} <span className="unit">{base}</span>
                </span>
                <span className="ticket-summary-sep">·</span>
                <span className="num">
                  {fmt(estimated.quote, 2)} <span className="unit">{quote}</span>
                </span>
              </>
            ) : (
              <span className="faint">输入数量后显示换算</span>
            )}
          </div>
        </div>

        {/* Step 3.5 — protective exits. Spot can only open long, so a sell
            there is an exit and carries no guard; futures may open a short
            with a sell and its stop/target sit on the opposite side. */}
        {(side === "buy" || market !== "spot" || hasPosition) && (
          <div className="field">
            <label className="field-label" htmlFor="stop-input">
              <span>止损 / 止盈</span>
              <em>
                {opening ? "可选，成交后自动平仓" : "该持仓已存在的保护"}
              </em>
            </label>
            <div className="protect-row">
              <input
                id="stop-input"
                className="input"
                type="text"
                inputMode="decimal"
                autoComplete="off"
                placeholder={short ? "止损价（高于开仓）" : "止损价"}
                value={stopLoss}
                onChange={(event) => setStopLoss(event.target.value)}
              />
              <input
                id="target-input"
                className="input"
                type="text"
                inputMode="decimal"
                autoComplete="off"
                placeholder={short ? "止盈价（低于开仓）" : "止盈价"}
                value={takeProfit}
                onChange={(event) => setTakeProfit(event.target.value)}
              />
            </div>
            <div className="protect-hint">
              <span>
                距开仓价{" "}
                <b className="num down">
                  {parseNumber(stopLoss) > 0 && referencePrice > 0
                    ? `${short ? "+" : "-"}${(
                        (Math.abs(referencePrice - parseNumber(stopLoss)) /
                          referencePrice) *
                        100
                      ).toFixed(2)}%`
                    : "—"}
                </b>{" "}
                /{" "}
                <b className="num up">
                  {parseNumber(takeProfit) > 0 && referencePrice > 0
                    ? `${short ? "-" : "+"}${(
                        (Math.abs(parseNumber(takeProfit) - referencePrice) /
                          referencePrice) *
                        100
                      ).toFixed(2)}%`
                    : "—"}
                </b>
              </span>
              <span className="faint">按最新成交价触发</span>
            </div>
            {hasPosition && (
              <div className="protect-hint">
                <span className="faint">
                  {opening
                    ? "也可直接应用到当前持仓"
                    : "填写后应用到当前持仓"}
                </span>
                <button
                  type="button"
                  className="link-btn"
                  disabled={!parseNumber(stopLoss) && !parseNumber(takeProfit)}
                  onClick={() => void applyProtection()}
                >
                  应用到持仓
                </button>
              </div>
            )}
            {protection && (protection.stopLoss > 0 || protection.takeProfit > 0) && (
              <div className="protect-hint">
                <span>
                  当前已生效：
                  {protection.stopLoss > 0 ? (
                    <b className="num down"> 止损 {fmtPrice(protection.stopLoss)}</b>
                  ) : null}
                  {protection.takeProfit > 0 ? (
                    <b className="num up"> 止盈 {fmtPrice(protection.takeProfit)}</b>
                  ) : null}
                </span>
                <button type="button" className="link-btn" onClick={() => void clearProtection()}>
                  撤销
                </button>
              </div>
            )}
          </div>
        )}

        {/* Step 3.6 — a standalone trigger order. Unlike the SL/TP guard above
            it is not tied to a position: it can wait for a pullback entry or
            protect a limit order that has not filled yet. */}
        <div className="field">
          <label className="field-label" htmlFor="trigger-input">
            <span>条件单</span>
            <em>触发后按{orderType === "market" ? "市价" : "限价"}成交</em>
          </label>
          <div className="conditional-row">
            <div className="segmented">
              <button
                type="button"
                aria-pressed={triggerDirection === "below"}
                onClick={() => setTriggerDirection("below")}
              >
                跌破
              </button>
              <button
                type="button"
                aria-pressed={triggerDirection === "above"}
                onClick={() => setTriggerDirection("above")}
              >
                涨破
              </button>
            </div>
            <input
              id="trigger-input"
              className="input"
              type="text"
              inputMode="decimal"
              autoComplete="off"
              placeholder="触发价"
              value={trigger}
              onChange={(event) => setTrigger(event.target.value)}
            />
          </div>
          <div className="protect-hint">
            <span className="faint">
              {mode === "live"
                ? "实盘条件单暂未开放，请使用交易所自带的条件单"
                : `数量沿用上方填写值${
                    triggerDirection === "below" ? "（价格跌到触发价）" : "（价格涨到触发价）"
                  }`}
            </span>
            <button
              type="button"
              className="link-btn"
              disabled={conditionalBusy || mode === "live"}
              onClick={() => void submitConditional()}
            >
              {conditionalBusy ? "挂出中…" : "挂条件单"}
            </button>
          </div>
        </div>

        {/* Step 4 — fees and the risk verdict. */}
        <div className="preview">
          <div className="preview-row">
            <span>预估手续费</span>
            <strong>
              {preview
                ? `${fmt(preview.fee_estimate, 4)} ${quote}`
                : estimated
                  ? `${fmt(estimated.quote * feeRate, 4)} ${quote}`
                  : "—"}
            </strong>
          </div>
          <div className="preview-row">
            <span>交易所数量</span>
            <strong>
              {preview ? `${fmt(preview.exchange_amount, 6)} ${base}` : "—"}
            </strong>
          </div>
          {previewError ? (
            <div className="verdict bad">无法预览：{previewError}</div>
          ) : preview ? (
            <div className={`verdict ${preview.allowed ? "ok" : "bad"}`}>
              {preview.allowed ? "风控通过" : `风控拒绝：${preview.reason}`}
            </div>
          ) : (
            <div className="verdict">填写数量后显示风控校验结果</div>
          )}
        </div>
      </div>

      {flash && <div className={`flash ${flash.tone}`}>{flash.text}</div>}

      {/* Step 4.5 — an optional second opinion. Advisory only: the button below
          submits whether or not this ever returns, and a failure here must not
          block an order. */}
      <div className="ai-prejudge">
        <button
          type="button"
          className="link-btn"
          disabled={prejudgeBusy || !preview}
          onClick={() => void askPrejudge()}
        >
          {prejudgeBusy ? "AI 预判中…" : "AI 预判这笔委托"}
        </button>
        {prejudgeText && <div className="ai-text ai-prejudge-text">{prejudgeText}</div>}
        {prejudgeError && <div className="faint">{prejudgeError}</div>}
      </div>

      {/* Step 5 — one button, coloured by direction. */}
      <div className="actions">
        <button
          type="button"
          className={`btn ${side === "buy" ? "buy" : "sell"}`}
          disabled={!canSubmit}
          onClick={submit}
        >
          {busy ? "提交中…" : side === "buy" ? `买入 ${base}` : `卖出 ${base}`}
        </button>
      </div>

      <div className="ticket-note">
        <div className="paper-line">
          <span>
            {mode === "live" ? "可用余额" : "纸面余额"}{" "}
            <b className="num">{fmt(cash, 2)}</b> {quote}
          </span>
          <span title="当前持仓已平掉那部分的盈亏（含手续费）；新开仓从 0 开始">
            本仓已实现{" "}
            <b className={`num ${realizedPnl >= 0 ? "up" : "down"}`}>
              {fmt(realizedPnl, 2)}
            </b>
          </span>
          {mode === "paper" ? (
            <button type="button" className="link-btn" onClick={() => void onResetPaper()}>
              重置
            </button>
          ) : (
            <span className="faint">实盘 · 余额来自交易所</span>
          )}
        </div>
        {mode === "live"
          ? "实盘交易：委托会发送到交易所，成交与盈亏由交易所结算。"
          : "纸面交易：使用本地模拟资金撮合，不会向交易所发送订单。"}
      </div>
    </div>
  );
}
