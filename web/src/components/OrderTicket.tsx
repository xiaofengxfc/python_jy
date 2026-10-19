import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { fmt, fmtPrice, parseNumber } from "../format";
import type { OrderPreview, OrderType, Side, SizeMode } from "../types";

interface Props {
  market: string;
  symbol: string;
  base: string;
  quote: string;
  lastPrice: number;
  cash: number;
  positionQty: number;
  realizedPnl: number;
  feeRate: number;
  pickedPrice: number | null;
  onPickedPriceConsumed: () => void;
  onFilled: () => void;
  onResetPaper: () => void | Promise<void>;
}

interface Flash {
  tone: "ok" | "err";
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
  feeRate,
  pickedPrice,
  onPickedPriceConsumed,
  onFilled,
  onResetPaper,
}: Props) {
  const [side, setSide] = useState<Side>("buy");
  const [orderType, setOrderType] = useState<OrderType>("limit");
  const [sizeMode, setSizeMode] = useState<SizeMode>("quote");
  const [price, setPrice] = useState("");
  const [size, setSize] = useState("");
  const [preview, setPreview] = useState<OrderPreview | null>(null);
  const [previewError, setPreviewError] = useState("");
  const [flash, setFlash] = useState<Flash | null>(null);
  const [busy, setBusy] = useState(false);
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
    setSize(sizeMode === "quote" ? readLastQuote() : "");
    setPreview(null);
    setPreviewError("");
    setFlash(null);
    // `sizeMode` is read as it stands when the symbol changes; depending on it
    // would wipe the form every time the user flips the unit.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbol, market]);

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

    setBusy(true);
    setFlash(null);
    try {
      const result = await api<{ order: Record<string, unknown> }>("/api/orders", {
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
      const order = result.order ?? {};
      const filled = Number(order.average ?? order.price ?? referencePrice);
      setFlash({
        tone: "ok",
        text: `${side === "buy" ? "买入" : "卖出"}成功：${fmt(
          Number(order.amount ?? 0),
          6,
        )} ${base} @ ${fmtPrice(filled)}`,
      });
      if (sizeMode === "quote") writeLastQuote(String(amount));
      setSize("");
      setPreview(null);
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
            纸面余额 <b className="num">{fmt(cash, 2)}</b> {quote}
          </span>
          <span>
            已实现{" "}
            <b className={`num ${realizedPnl >= 0 ? "up" : "down"}`}>
              {fmt(realizedPnl, 2)}
            </b>
          </span>
          <button type="button" className="link-btn" onClick={() => void onResetPaper()}>
            重置
          </button>
        </div>
        纸面交易：使用本地模拟资金撮合，不会向交易所发送订单。
      </div>
    </div>
  );
}
