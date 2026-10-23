import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, query } from "../api";
import { fmt, fmtPrice, parseNumber } from "../format";
import { createSectionSaver } from "../settingsAutoSave";
import type {
  OrderPreview,
  OrderType,
  Protection,
  Side,
  SizeMode,
  TradeSettings,
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
  feeRate: number;
  pickedPrice: number | null;
  /** Stop / take-profit orders the venue already holds for this pair. */
  protection?: Protection | null;
  /** mainnet or testnet: decides whether a click needs a second confirm. */
  network: string;
  onPickedPriceConsumed: () => void;
  onFilled: () => void;
}

interface Flash {
  tone: "ok" | "err" | "warn";
  text: string;
}

/** Last size the user actually typed, remembered across symbol switches so
 * the common case (trade 100 USDT of something else) is one click away. */
const LAST_QUOTE_KEY = "bitrader.order.quote";

/** 杠杆只收 1–125 的整数（交易所给的上限）。真闸是 risk.max_leverage，
 *  这里只是输入框自己的边界，超了提示一句，不改用户填的数。 */
const MAX_LEVERAGE = 125;

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
  protection,
  network,
  onPickedPriceConsumed,
  onFilled,
}: Props) {
  const [side, setSide] = useState<Side>("buy");
  const [orderType, setOrderType] = useState<OrderType>("limit");
  const [sizeMode, setSizeMode] = useState<SizeMode>("quote");
  const [price, setPrice] = useState("");
  const [size, setSize] = useState(() => {
    const remembered = readLastQuote();
    return remembered;
  });
  const [stopLoss, setStopLoss] = useState("");
  const [takeProfit, setTakeProfit] = useState("");
  // Trigger order: waits for the market to cross a level before it is sent.
  const [trigger, setTrigger] = useState("");
  const [triggerDirection, setTriggerDirection] = useState<"below" | "above">("below");
  const [conditionalBusy, setConditionalBusy] = useState(false);
  const [preview, setPreview] = useState<OrderPreview | null>(null);
  const [previewError, setPreviewError] = useState("");
  const [flash, setFlash] = useState<Flash | null>(null);
  const [busy, setBusy] = useState(false);
  /** Mainnet asks once per order; the testnet never does. */
  const [confirmOpen, setConfirmOpen] = useState(false);
  /** Second opinion on the pending order. Cache lives on the server, keyed by
   * candle and size bucket, so clicking twice on the same bar is free. */
  const [prejudgeText, setPrejudgeText] = useState("");
  const [prejudgeError, setPrejudgeError] = useState("");
  const [prejudgeBusy, setPrejudgeBusy] = useState(false);
  const requestId = useRef(0);

  const live = network === "mainnet";

  /** 杠杆按市场记：settings.trade.leverage = {"usdm": 10, "coinm": 5}。
   *  改动即存（500ms 攒一次），刷新页面还在。 */
  const tradeSaver = useRef(createSectionSaver("trade"));
  const [leverageByMarket, setLeverageByMarket] = useState<Record<string, number>>({});
  const [leverageText, setLeverageText] = useState("1");
  /** risk.max_leverage 是全局一个数，但读数按市场各记一份：切市场先回到「还在读」，
   *  别拿 usdm 那次读到的结果去说 coinm。 */
  const [capByMarket, setCapByMarket] = useState<
    Record<string, { state: "known" | "unknown"; cap: number | null }>
  >({});
  const capReading = capByMarket[market];
  /** risk.max_leverage：只用来提示「超过会按上限处理」，不替用户改数。 */
  const maxLeverage = capReading?.cap ?? null;
  /** 上限的读取状态：读不到就是「不知道上限」，不拿 1 倍冒充。新市场还没读就是 loading。 */
  const maxLeverageState: "loading" | "known" | "unknown" = capReading?.state ?? "loading";
  /** 上一次下单回执里的 leverage_note：只对当时那张票有效，见 leverageNoticeText。 */
  const [leverageNotice, setLeverageNotice] = useState<{ text: string; ticket: string } | null>(
    null,
  );

  /** 读一遍设置：这个市场记住的杠杆 + risk.max_leverage（记在发起这次读的市场名下）。 */
  const loadTradeSettings = useCallback(async () => {
    const target = market;
    const remember = (state: "known" | "unknown", cap: number | null) =>
      setCapByMarket((prev) => ({ ...prev, [target]: { state, cap } }));
    try {
      const data = await api<{
        settings?: { trade?: TradeSettings; risk?: { max_leverage?: number } };
      }>("/api/settings");
      setLeverageByMarket(data?.settings?.trade?.leverage ?? {});
      const cap = data?.settings?.risk?.max_leverage;
      if (typeof cap === "number" && Number.isFinite(cap)) {
        remember("known", cap);
      } else {
        remember("unknown", null);
      }
    } catch {
      // 读不到就说读不到：栏还在、能下单，但别装自己知道上限。
      remember("unknown", null);
    }
  }, [market]);

  // 打开面板读一次；换市场再读一次（上限与那个市场记住的倍数都以设置为准）。
  useEffect(() => {
    void loadTradeSettings();
  }, [loadTradeSettings, market]);

  // 设置页改完回来（窗口拿回焦点 / 页面重新可见）再读一次，上限跟着变。
  useEffect(() => {
    const refresh = () => void loadTradeSettings();
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      window.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, [loadTradeSettings]);

  // 换市场（现货↔合约）就换成那个市场记住的倍数；没记过就是 1。
  useEffect(() => {
    const stored = Number(leverageByMarket[market]);
    setLeverageText(String(Number.isFinite(stored) && stored >= 1 ? stored : 1));
  }, [market, leverageByMarket]);

  // 离开下单框时把还没写的杠杆补上。
  useEffect(() => () => tradeSaver.current.flush(), []);

  const changeLeverage = (raw: string) => {
    setLeverageText(raw);
    const trimmed = raw.trim();
    const parsed = Math.trunc(Number(trimmed));
    if (!trimmed || !Number.isFinite(parsed) || parsed < 1 || parsed > MAX_LEVERAGE) return;
    const next = { ...leverageByMarket, [market]: parsed };
    setLeverageByMarket(next);
    tradeSaver.current.queue({ leverage: next });
  };

  /** 输入框里那个整数，非法时按 1（不夹到 risk 上限：那是后端收口的事）。 */
  const leverageValue = useMemo(() => {
    const parsed = Math.trunc(Number(leverageText));
    return Number.isFinite(parsed) && parsed >= 1 ? parsed : 1;
  }, [leverageText]);

  /** 一张「票」的记号：市场 + 交易对 + 杠杆 + 数量。任一项变了，上一条回执说明就作废——
   *  别把 usdm 的「这次按 10x 下单」留在现货票上，也别留在改过的数量上。
   *  现货不显示杠杆栏，换过去记号一样对不上，照样作废。 */
  const ticketKey = (quantity: string) => `${market}|${symbol}|${leverageValue}|${quantity}`;
  const ticket = ticketKey(size);
  /** 只有票没变时才认那条说明。 */
  const leverageNoticeText =
    leverageNotice && leverageNotice.ticket === ticket ? leverageNotice.text : "";

  /** 上限读不到时的一句中性提示：说清「不知道上限、后端会收口」，别装知道。 */
  const maxLeverageHint = useMemo(() => {
    if (market === "spot") return "";
    if (maxLeverageState !== "unknown") return "";
    return "风控上限还没读到（设置没取回来）：先按你要的倍数下单，后端会按 risk.max_leverage 收口并在回执里说明。";
  }, [market, maxLeverageState]);

  /** 后端把收口结果写在 leverage_note 里：预览显示，成交后留着，
   *  直到下一次下单、或者这张票被改动（见 leverageNoticeText）。 */
  const leverageHint = leverageNoticeText || preview?.leverage_note || "";

  const leverageNote = useMemo(() => {
    if (market === "spot") return "";
    if (leverageValue > MAX_LEVERAGE) {
      return `杠杆只收 1 到 ${MAX_LEVERAGE} 的整数，超过按 ${MAX_LEVERAGE} 倍收口。`;
    }
    if (maxLeverage != null && leverageValue > maxLeverage) {
      return `上限 ${maxLeverage} 倍，超过会按上限处理`;
    }
    return "";
  }, [market, leverageValue, maxLeverage]);

  /** 风控那一片那条提示：后端的收口说明优先，其次是「上限读不到」的中性提示。
   *  本地「超过 125 按 125」那条留在杠杆栏里，别在同一屏说两遍。 */
  const leverageTip = leverageHint || maxLeverageHint;

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
        ? readLastQuote()
        : "",
    );
    setStopLoss("");
    setTakeProfit("");
    setTrigger("");
    setPreview(null);
    setPreviewError("");
    setFlash(null);
    // The unit is read as it stands when the symbol changes; depending on it
    // would wipe the form every time the user flips the unit.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbol, market]);


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
  // and can therefore carry a guard.
  const opening = side === "buy" || market !== "spot";
  const short = side === "sell" && market !== "spot";

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
            // 杠杆只对合约有意义：现货不发这个字段。
            ...(market === "spot" ? {} : { leverage: leverageValue }),
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
  }, [market, symbol, side, orderType, sizeMode, size, referencePrice, leverageValue]);

  /** Ask the assistant whether this order makes sense right now. Deliberately
   * separate from the submit path: it must be possible to ignore it, and it
   * must never be able to block the order. */
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
      setPrejudgeError(error instanceof Error ? error.message : String(error));
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
    return side === "buy" ? (referencePrice > 0 ? cash / referencePrice : 0) : positionQty;
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

  /** 交易所的最小下单量和最小名义额，preview 已经报上来了。低于它的单必然被交易所拒
   *  （实测 0.002 ETH = 4.77 USDT 被 -1013 Filter failure: NOTIONAL 拒），与其发出去
   *  再把交易所报文念一遍，不如在这里先拦下来。拿不到这两个数就不拦。 */
  const minimumProblem = (amount: number): string => {
    const minAmount = preview?.min_amount ?? null;
    const minNotional = preview?.min_notional ?? null;
    if (!minAmount && !minNotional) return "";
    const baseAmount = preview?.amount_base ?? (sizeMode === "amount" ? amount : amount / (referencePrice || 1));
    const notional = preview?.quote_notional ?? (sizeMode === "quote" ? amount : amount * referencePrice);
    if (minAmount && baseAmount > 0 && baseAmount < minAmount) {
      return `最小下单数量 ${fmt(minAmount, 8)} ${base}，这一单只有 ${fmt(baseAmount, 8)} ${base}，交易所会直接拒。`;
    }
    if (minNotional && notional > 0 && notional < minNotional) {
      return `最小下单金额 ${fmt(minNotional, 2)} ${quote}，这一单只有 ${fmt(notional, 2)} ${quote}，交易所会直接拒。`;
    }
    return "";
  };

  /** 填够数就把话说完：低于最小值的提示不用等到点提交才出现。
   *  提交那一步仍然拦一次（send / submitConditional），这里只是让它先看得见。 */
  const minimumWarning = useMemo(() => {
    const amount = parseNumber(size);
    if (!symbol || !(amount > 0)) return "";
    return minimumProblem(amount);
  }, [symbol, size, preview, sizeMode, referencePrice, base, quote]);

  const send = async () => {
    const amount = parseNumber(size);
    if (!symbol) return setFlash({ tone: "err", text: "请先选择交易对。" });
    if (amount <= 0) return setFlash({ tone: "err", text: "请填写数量或金额。" });
    const tooSmall = minimumProblem(amount);
    if (tooSmall) return setFlash({ tone: "err", text: tooSmall });
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
    setLeverageNotice(null);
    try {
      const result = await api<{
        order: Record<string, unknown>;
        warnings?: string[];
        leverage_note?: string | null;
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
          // 杠杆只对合约有意义：现货不发这个字段。
          ...(market === "spot" ? {} : { leverage: leverageValue }),
          // Only mainnet needs it, and only this panel can honestly assert the
          // user was asked — it is the one that showed the dialog.
          confirm_live: live,
        }),
      });
      const order = result.order ?? {};
      // 回执先说人话：filled > 0 才算成交。限价单刚受理时 filled 是 0、status 是 open，
      // 那不是「成交」，是「挂上去了、还没成交」——把受理写成成交，人就以为钱已经换成了币。
      const filledQty = Number(order.filled ?? 0);
      const orderStatus = String(order.status ?? "").toLowerCase();
      const price = Number(order.average ?? order.price ?? referencePrice);
      const quantity = Number(order.amount ?? 0) > 0 ? Number(order.amount) : filledQty;
      const guard = [
        opening && stop > 0 ? `止损 ${fmtPrice(stop)}` : "",
        opening && target > 0 ? `止盈 ${fmtPrice(target)}` : "",
      ]
        .filter(Boolean)
        .join(" · ");
      const warnings = result.warnings ?? [];
      const resting = ["open", "new", "pending", "accepted"].includes(orderStatus);
      const filledText =
        filledQty > 0
          ? `${side === "buy" ? "买入成交" : "卖出成交"}：${fmt(filledQty, 6)} ${base} @ ${fmtPrice(price)}` +
            (guard ? `（${guard}）` : "")
          : resting
            ? `已挂单，等待成交：${fmt(quantity, 6)} ${base} @ ${fmtPrice(price)}` +
              (guard ? `（${guard}）` : "") +
              "——交易所还没成交，成交之后才出现在持仓与成交记录里"
            : `已提交，状态未知（回执状态 ${orderStatus || "没给出"}）：${fmt(quantity, 6)} ${base} @ ${fmtPrice(price)}` +
              "——去交易所或持仓里核实一下，别当已成交";
      setFlash(
        warnings.length || filledQty <= 0
          ? { tone: "warn", text: warnings.length ? `${filledText}。${warnings.join("；")}` : filledText }
          : { tone: "ok", text: filledText },
      );
      // 杠杆被收口时后端在回执里说清是撞了哪一条上限：留在面板上，不随 flash 一起消失。
      // 这一单成功后会清空数量框，所以记账用清空后的那张票，否则说明刚落地就被自己擦掉。
      setLeverageNotice(result.leverage_note ? { text: String(result.leverage_note), ticket: ticketKey("") } : null);
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

  /** Arm a standalone trigger order: nothing reaches the exchange until the
   * price crosses the level, then it becomes a market order. Futures only —
   * the block below explains that instead of silently doing nothing. */
  const submitConditional = async () => {
    const amount = parseNumber(size);
    const level = parseNumber(trigger);
    if (amount <= 0) return setFlash({ tone: "err", text: "请填写数量或金额。" });
    if (level <= 0) return setFlash({ tone: "err", text: "请填写触发价。" });
    // 触发单成交时也是市价单，最小量与最小名义额一样管得住它。
    const tooSmall = minimumProblem(amount);
    if (tooSmall) return setFlash({ tone: "err", text: tooSmall });
    setConditionalBusy(true);
    setFlash(null);
    try {
      const result = await api<{
        trigger: number;
        amount_base: number;
        kind: string;
      }>("/api/orders/conditional", {
        method: "POST",
        body: JSON.stringify({
          market,
          symbol,
          side,
          order_type: "market",
          size_mode: sizeMode,
          size: amount,
          trigger: level,
          direction: triggerDirection,
          reduce_only: side === "sell" && positionQty > 0,
          // 条件单也是真委托，合约侧同样按这里的倍数开。
          ...(market === "spot" ? {} : { leverage: leverageValue }),
          confirm_live: live,
        }),
      });
      setFlash({
        tone: "ok",
        text:
          "条件单已挂出：" +
          (triggerDirection === "below" ? "跌破 " : "涨破 ") +
          fmtPrice(result.trigger) +
          " 后" +
          (side === "buy" ? "买入 " : "卖出 ") +
          fmt(result.amount_base, 6) +
          " " +
          base,
      });
      setTrigger("");
      onFilled();
    } catch (error) {
      setFlash({ tone: "err", text: error instanceof Error ? error.message : String(error) });
    } finally {
      setConditionalBusy(false);
    }
  };

  /** Move the levels above onto the position that is already open. The server
   * knows which side that position is, so it is the one that validates them. */
  const applyProtection = async () => {
    const stop = parseNumber(stopLoss);
    const target = parseNumber(takeProfit);
    if (!stop && !target) return setFlash({ tone: "err", text: "请填写止损或止盈价。" });
    setBusy(true);
    setFlash(null);
    try {
      await api("/api/positions/protection", {
        method: "PUT",
        body: JSON.stringify({
          symbol,
          market,
          stop_loss: stop > 0 ? stop : null,
          take_profit: target > 0 ? target : null,
          confirm_live: live,
        }),
      });
      const parts = [
        stop > 0 ? "止损 " + fmtPrice(stop) : "",
        target > 0 ? "止盈 " + fmtPrice(target) : "",
      ].filter(Boolean);
      setFlash({ tone: "ok", text: "已挂到持仓：" + parts.join(" · ") });
      onFilled();
    } catch (error) {
      setFlash({ tone: "err", text: error instanceof Error ? error.message : String(error) });
    } finally {
      setBusy(false);
    }
  };

  const clearProtection = async () => {
    setBusy(true);
    setFlash(null);
    try {
      await api("/api/positions/protection?" + query({ symbol, market }), {
        method: "DELETE",
      });
      setFlash({ tone: "ok", text: "已撤销该交易对的止损/止盈。" });
      onFilled();
    } catch (error) {
      setFlash({ tone: "err", text: error instanceof Error ? error.message : String(error) });
    } finally {
      setBusy(false);
    }
  };

  /** One button, one dialog on mainnet: send or don't. */
  const submit = () => {
    if (live) {
      setConfirmOpen(true);
      return;
    }
    void send();
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
            <em>{orderType === "market" ? "以最新价成交" : quote}</em>
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

        {/* 杠杆：现货没有借币这回事，整栏不显示。整数 1–125，改动即记到设置里。 */}
        {market !== "spot" && (
          <div className="field">
            <label className="field-label" htmlFor="leverage-input">
              <span>杠杆</span>
              <em>倍数，只对合约生效</em>
            </label>
            <input
              id="leverage-input"
              className="input"
              type="number"
              inputMode="numeric"
              min={1}
              max={MAX_LEVERAGE}
              step={1}
              value={leverageText}
              onChange={(event) => changeLeverage(event.target.value)}
            />
            {leverageNote && (
              <div className="protect-hint">
                <span className="faint">{leverageNote}</span>
              </div>
            )}
          </div>
        )}

        {/* Step 3.5 — protective exits. They are sent with the entry and the
            venue holds them as reduce-only stop orders, so they survive this
            app being closed. Spot cannot do it at all: Binance has no
            reduce-only stop for spot balances. */}
        {opening && market !== "spot" && (
          <div className="field">
            <label className="field-label" htmlFor="stop-input">
              <span>止损 / 止盈</span>
              <em>可选，成交后在交易所挂保护单</em>
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
                    ? (short ? "+" : "-") +
                      (
                        (Math.abs(referencePrice - parseNumber(stopLoss)) /
                          referencePrice) *
                        100
                      ).toFixed(2) +
                      "%"
                    : "—"}
                </b>{" "}
                /{" "}
                <b className="num up">
                  {parseNumber(takeProfit) > 0 && referencePrice > 0
                    ? (short ? "-" : "+") +
                      (
                        (Math.abs(parseNumber(takeProfit) - referencePrice) /
                          referencePrice) *
                        100
                      ).toFixed(2) +
                      "%"
                    : "—"}
                </b>
              </span>
              <span className="faint">按交易所触发价成交</span>
            </div>
            {positionQty !== 0 && (
              <div className="protect-hint">
                <span className="faint">也可以直接改挂到当前持仓</span>
                <button
                  type="button"
                  className="link-btn"
                  disabled={busy || (!parseNumber(stopLoss) && !parseNumber(takeProfit))}
                  onClick={() => void applyProtection()}
                >
                  应用到持仓
                </button>
              </div>
            )}
            {protection && (protection.stop_loss > 0 || protection.take_profit > 0) && (
              <div className="protect-hint">
                <span>
                  交易所现有保护：
                  {protection.stop_loss > 0 ? (
                    <b className="num down"> 止损 {fmtPrice(protection.stop_loss)}</b>
                  ) : null}
                  {protection.take_profit > 0 ? (
                    <b className="num up"> 止盈 {fmtPrice(protection.take_profit)}</b>
                  ) : null}
                </span>
                <button
                  type="button"
                  className="link-btn"
                  disabled={busy}
                  onClick={() => void clearProtection()}
                >
                  撤销
                </button>
              </div>
            )}
          </div>
        )}

        {/* Step 3.6 — a standalone trigger order. It is not tied to a position,
            so it can wait for a pullback entry or pre-place an exit. Spot has
            no venue-side trigger order at all, so the block says so rather than
            pretending to arm one. */}
        <div className="field">
          <label className="field-label" htmlFor="trigger-input">
            <span>条件单</span>
            <em>{market === "spot" ? "仅合约市场支持" : "触发后按市价成交"}</em>
          </label>
          {market === "spot" ? (
            <div className="protect-hint">
              <span className="faint">
                现货没有交易所侧条件单，请用币安自带的「止盈止损」下单。
              </span>
            </div>
          ) : (
            <>
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
                  数量沿用上方填写值（
                  {triggerDirection === "below" ? "跌到触发价才下单" : "涨到触发价才下单"}
                  ）
                </span>
                <button
                  type="button"
                  className="link-btn"
                  disabled={conditionalBusy || !trigger || !size}
                  onClick={() => void submitConditional()}
                >
                  {conditionalBusy ? "挂出中…" : "挂条件单"}
                </button>
              </div>
            </>
          )}
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
          {/* 交易所的最小量/最小名义额：只报数，不猜（拿不到就不显示）。 */}
          {(preview?.min_amount || preview?.min_notional) && (
            <div className="preview-row">
              <span>交易所最低</span>
              <strong className="faint">
                {[
                  preview?.min_amount ? `${fmt(preview.min_amount, 8)} ${base}` : "",
                  preview?.min_notional ? `${fmt(preview.min_notional, 2)} ${quote}` : "",
                ]
                  .filter(Boolean)
                  .join(" ／ ")}
              </strong>
            </div>
          )}
          {/* 低于最小值当场说清楚：别让用户点一次提交才知道交易所会拒。 */}
          {minimumWarning && <div className="flash err">{minimumWarning}</div>}
          {/* 杠杆被风控收口：后端 leverage_note 原样显示；上限读不到时给中性提示。 */}
          {leverageTip && (
            <div className="protect-hint">
              <span className="faint">{leverageTip}</span>
            </div>
          )}
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
        <div className="ticket-line">
          <span>
            可用余额 <b className="num">{fmt(cash, 2)}</b> {quote}
          </span>
          <span title="本仓的已实现盈亏（含手续费）">
            本仓已实现{" "}
            <b className={`num ${realizedPnl >= 0 ? "up" : "down"}`}>
              {fmt(realizedPnl, 2)}
            </b>
          </span>
          <span className="faint">{live ? "主网 · 真实资金" : "测试网 · 模拟资金"}</span>
        </div>
        {market === "spot"
          ? "现货委托直接发到交易所。现货没有交易所侧止损止盈，需要的话请用合约市场。"
          : "委托直接发到交易所。保护单由交易所持有，关掉本程序也不失效。"}
      </div>

      {confirmOpen && (
        <div className="modal-mask" role="presentation">
          <div className="modal" role="dialog" aria-modal="true" aria-label="确认下单">
            <div className="modal-title">确认发送到主网？</div>
            <p className="modal-text">
              这笔委托会以<b>真实资金</b>在币安主网成交，盈亏由交易所结算。
            </p>
            <p className="modal-text faint">
              {side === "buy" ? "买入" : "卖出"}{" "}
              {sizeMode === "quote"
                ? `${fmt(parseNumber(size), 2)} ${quote}`
                : `${fmt(parseNumber(size), 6)} ${base}`}
              {referencePrice > 0 ? ` · 参考价 ${fmtPrice(referencePrice)}` : ""}
            </p>
            <div className="modal-actions">
              <button
                type="button"
                className="btn ghost"
                onClick={() => setConfirmOpen(false)}
              >
                取消
              </button>
              <button
                type="button"
                className={`btn ${side === "buy" ? "buy" : "sell"}`}
                onClick={() => {
                  setConfirmOpen(false);
                  void send();
                }}
              >
                我已确认，发送
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
