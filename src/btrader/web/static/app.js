/* ============================================================
   biTrader Web — page logic
   No framework, no build step: open the page, edit, reload.
   ============================================================ */
"use strict";

/* ------------------------------------------------------------ state */
const state = {
  market: "spot",
  symbol: "BTC/USDT",
  timeframe: "15m",
  orderType: "limit",
  sizeMode: "amount",
  markets: [],
  symbols: [],
  candles: [],
  lastPrice: 0,
  book: { bids: [], asks: [] },
  account: null,
  drawerTab: "open",
  drawerOpen: false,
  // Bumped on every symbol/market change so a slow response for the previous
  // symbol cannot overwrite the chart after the user has moved on.
  requestId: 0,
};

const TIMEFRAMES = [
  ["1m", "1分"],
  ["5m", "5分"],
  ["15m", "15分"],
  ["1h", "1时"],
  ["4h", "4时"],
  ["1d", "1日"],
];

const $ = (id) => document.getElementById(id);

/* -------------------------------------------------------------- utils */
function fmt(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  return Number(value).toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function fmtPrice(value) {
  const n = Math.abs(Number(value) || 0);
  if (n >= 1000) return fmt(value, 2);
  if (n >= 1) return fmt(value, 2);
  if (n >= 0.01) return fmt(value, 4);
  return fmt(value, 6);
}

function fmtCompact(value) {
  const n = Number(value) || 0;
  if (n >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(2) + "K";
  return n.toFixed(2);
}

function decideDigits(price) {
  const n = Math.abs(Number(price) || 0);
  if (n >= 1000) return 2;
  if (n >= 1) return 2;
  if (n >= 0.01) return 4;
  return 6;
}

async function api(path, options) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const text = await response.text();
  let body = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = text;
  }
  if (!response.ok) {
    const detail = (body && body.detail) || body || response.statusText;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return body;
}

function setBanner(message) {
  const banner = $("banner");
  if (!message) {
    banner.dataset.show = "false";
    return;
  }
  $("bannerText").textContent = message;
  banner.dataset.show = "true";
}

/* ---------------------------------------------------------- segmented */
function buildSegmented(container, items, active, onPick, keyName) {
  container.replaceChildren();
  for (const [value, label] of items) {
    const button = document.createElement("button");
    button.textContent = label;
    button.dataset[keyName] = value;
    button.setAttribute("aria-pressed", String(value === active));
    button.addEventListener("click", () => onPick(value));
    container.appendChild(button);
  }
}

function markSegmented(container, keyName, active) {
  for (const button of container.querySelectorAll("button")) {
    button.setAttribute("aria-pressed", String(button.dataset[keyName] === active));
  }
}

/* -------------------------------------------------------------- chrome */
function switchPage(page) {
  const titles = {
    trading: "交易",
    portfolio: "资产",
    auto: "自动交易",
    backtest: "回测",
    settings: "设置",
  };
  $("pageTitle").textContent = titles[page] || "交易";
  for (const button of document.querySelectorAll(".nav-item")) {
    if (button.dataset.page === page) {
      button.setAttribute("aria-current", "page");
    } else {
      button.removeAttribute("aria-current");
    }
  }
  for (const section of document.querySelectorAll(".page")) {
    section.hidden = section.id !== `page-${page}`;
  }
  if (page === "trading") drawChart();
}

/* --------------------------------------------------------------- boot */
async function boot() {
  buildSegmented(
    $("timeframes"),
    TIMEFRAMES,
    state.timeframe,
    (value) => {
      state.timeframe = value;
      markSegmented($("timeframes"), "timeframe", value);
      loadKlines({ reset: true });
    },
    "timeframe"
  );

  $("nav").addEventListener("click", (event) => {
    const button = event.target.closest(".nav-item");
    if (button) switchPage(button.dataset.page);
  });

  $("symbolSelect").addEventListener("change", (event) => {
    state.symbol = event.target.value;
    state.candles = [];
    hideFlash();
    loadKlines({ reset: true });
    loadTicker();
    loadBook();
  });

  $("orderType").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-type]");
    if (!button) return;
    state.orderType = button.dataset.type;
    markSegmented($("orderType"), "type", state.orderType);
    $("priceInput").disabled = state.orderType === "market";
    if (state.orderType === "market") $("priceInput").value = "";
    updateEstimate();
  });

  $("sizeMode").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-mode]");
    if (!button) return;
    state.sizeMode = button.dataset.mode;
    markSegmented($("sizeMode"), "mode", state.sizeMode);
    $("estimateLabel").textContent =
      state.sizeMode === "quote" ? "预计数量" : "预计金额";
    updateEstimate();
  });

  for (const input of ["priceInput", "sizeInput"]) {
    $(input).addEventListener("input", updateEstimate);
  }

  $("buyBtn").addEventListener("click", () => submitOrder("buy"));
  $("sellBtn").addEventListener("click", () => submitOrder("sell"));

  $("drawerToggle").addEventListener("click", () => {
    state.drawerOpen = !state.drawerOpen;
    $("drawer").dataset.open = String(state.drawerOpen);
    $("drawerToggle").setAttribute("aria-expanded", String(state.drawerOpen));
    if (state.drawerOpen) renderDrawer();
  });

  document.querySelector(".drawer__tabs").addEventListener("click", (event) => {
    const tab = event.target.closest(".drawer__tab");
    if (!tab) return;
    state.drawerTab = tab.dataset.tab;
    for (const button of document.querySelectorAll(".drawer__tab")) {
      button.setAttribute("aria-selected", String(button === tab));
    }
    renderDrawer();
  });

  window.addEventListener("resize", debounce(drawChart, 120));

  try {
    const health = await api("/api/health");
    state.markets = health.markets;
    state.market = health.market;
    buildSegmented(
      $("markets"),
      health.markets.map((m) => [m.value, m.label]),
      state.market,
      (value) => {
        state.market = value;
        markSegmented($("markets"), "market", value);
        $("marketMeta").textContent =
          health.markets.find((m) => m.value === value)?.label || value;
        state.candles = [];
        hideFlash();
        loadSymbols();
      },
      "market"
    );
    $("marketMeta").textContent =
      health.markets.find((m) => m.value === state.market)?.label || state.market;
  } catch (error) {
    setBanner(`无法连接本地服务：${error.message}`);
  }

  await loadSymbols();
  loadKlines({ reset: true });
  loadTicker();
  loadBook();
  refreshAccount();

  setInterval(() => document.hidden || pollFast(), 2000);
  setInterval(() => document.hidden || refreshAccount(), 5000);
  setInterval(() => document.hidden || loadKlines({ reset: false }), 15000);
}

function debounce(fn, wait) {
  let handle = null;
  return (...args) => {
    clearTimeout(handle);
    handle = setTimeout(() => fn(...args), wait);
  };
}

/* ------------------------------------------------------------ symbols */
async function loadSymbols() {
  try {
    const data = await api(`/api/symbols?market=${encodeURIComponent(state.market)}`);
    state.symbols = data.items || [];
    if (data.degraded) {
      setBanner(`行情加载受限，正在使用备用交易对列表：${data.error}`);
    } else {
      setBanner("");
      $("connMeta").textContent = "已连接";
    }
    const select = $("symbolSelect");
    select.replaceChildren();
    for (const item of state.symbols) {
      const option = document.createElement("option");
      option.value = item.symbol;
      option.textContent = item.display || item.symbol;
      select.appendChild(option);
    }
    const known = state.symbols.some((item) => item.symbol === state.symbol);
    state.symbol = known ? state.symbol : (state.symbols[0]?.symbol ?? "");
    if (state.symbol) select.value = state.symbol;
  } catch (error) {
    setBanner(`加载交易对失败：${error.message}`);
    $("connMeta").textContent = "离线";
    return;
  }
  await Promise.all([loadTicker(), loadBook(), loadKlines({ reset: true })]);
}

/* --------------------------------------------------------------- data */
async function loadKlines({ reset }) {
  if (!state.symbol) return;
  // Tag the request so a stale response for a previous symbol is discarded.
  const requestId = ++state.requestId;
  try {
    const query = new URLSearchParams({
      market: state.market,
      symbol: state.symbol,
      timeframe: state.timeframe,
      limit: "400",
    });
    const data = await api(`/api/klines?${query}`);
    if (requestId !== state.requestId) return;
    state.candles = data.candles || [];
    $("chartEmpty").hidden = state.candles.length > 0;
    if (state.candles.length) {
      const last = state.candles[state.candles.length - 1];
      updateLegend(last);
      if (!state.lastPrice) setLastPrice(last.c);
    }
    drawChart();
    if (reset) syncPricePlaceholder();
  } catch (error) {
    if (requestId !== state.requestId) return;
    state.candles = [];
    drawChart();
    $("chartEmpty").hidden = false;
    $("chartEmpty").textContent = `K 线加载失败：${error.message}`;
  }
}

async function loadTicker() {
  if (!state.symbol) return;
  try {
    const data = await api(
      `/api/ticker?market=${state.market}&symbol=${encodeURIComponent(state.symbol)}`
    );
    setLastPrice(data.last, data.change_pct);
    $("dayHigh").textContent = fmtPrice(data.high);
    $("dayLow").textContent = fmtPrice(data.low);
    $("dayVolume").textContent = fmtCompact(data.quote_volume || data.base_volume);
    syncPricePlaceholder();
    updateEstimate();
  } catch {
    /* a failed tick is not worth a banner; the next poll usually recovers */
  }
}

async function loadBook() {
  if (!state.symbol) return;
  try {
    const data = await api(
      `/api/orderbook?market=${state.market}&symbol=${encodeURIComponent(state.symbol)}&limit=10`
    );
    state.book = data;
    renderBook();
  } catch {
    /* ignore */
  }
}

function pollFast() {
  loadTicker();
  loadBook();
}

async function refreshAccount() {
  try {
    const account = await api("/api/account");
    state.account = account;
    $("accountEquity").textContent = fmt(account.cash, 2);
    $("accountDot").className = "dot live";
    const openCount = (account.orders || []).filter((o) => o.status === "open").length;
    $("drawerCount").textContent = String(openCount);
    if (state.drawerOpen) renderDrawer();
  } catch {
    $("accountDot").className = "dot off";
  }
}

/* ------------------------------------------------------------- render */
function setLastPrice(price, changePct) {
  state.lastPrice = Number(price) || 0;
  $("lastPrice").textContent = fmtPrice(state.lastPrice);
  const change = Number(changePct) || 0;
  $("priceChange").textContent = `${change >= 0 ? "+" : ""}${change.toFixed(2)}%`;
  $("priceChange").className = `symbol-change num ${change >= 0 ? "up" : "down"}`;
}

function updateLegend(candle) {
  if (!candle) return;
  $("legendOpen").textContent = fmtPrice(candle.o);
  $("legendHigh").textContent = fmtPrice(candle.h);
  $("legendLow").textContent = fmtPrice(candle.l);
  $("legendClose").textContent = fmtPrice(candle.c);
}

function renderBook() {
  const digits = decideDigits(state.lastPrice);
  fillSide($("bids"), state.book.bids, "bids", digits, true);
  fillSide($("asks"), state.book.asks, "asks", digits, false);
}

function fillSide(container, rows, side, digits, isBid) {
  container.replaceChildren();
  const list = (rows || []).slice(0, 8);
  const max = Math.max(...list.map((r) => Number(r[1]) || 0), 0.000001);
  for (const [price, qty] of list) {
    const row = document.createElement("div");
    row.className = "book__row";

    const bar = document.createElement("div");
    bar.className = "book__bar";
    bar.style.width = `${Math.min(100, ((Number(qty) || 0) / max) * 100)}%`;

    const priceEl = document.createElement("span");
    priceEl.className = isBid ? "up num" : "down num";
    priceEl.textContent = fmt(price, digits);

    const qtyEl = document.createElement("span");
    qtyEl.className = "num dim";
    qtyEl.textContent = fmt(qty, 4);

    row.append(bar, priceEl, qtyEl);
    container.appendChild(row);
  }
}

/* ------------------------------------------------------------- chart */
function drawChart() {
  const canvas = $("chart");
  if (!canvas) return;
  const parent = canvas.parentElement.getBoundingClientRect();
  const width = Math.max(240, Math.floor(parent.width) - 16);
  const height = Math.max(180, Math.floor(parent.height) - 12);
  const dpr = window.devicePixelRatio || 1;

  canvas.width = width * dpr;
  canvas.height = height * dpr;
  canvas.style.width = `${width}px`;
  canvas.style.height = `${height}px`;

  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);

  // Clearing and reloading leaves the canvas empty; drop the stale drawing
  // instead of leaving the previous symbol's candles on screen.
  if (!state.candles.length) return;

  const candles = state.candles;
  const visible = Math.min(candles.length, Math.max(40, Math.floor(width / 8)));
  const slice = candles.slice(-visible);

  const padding = { top: 10, right: 58, bottom: 22, left: 8 };
  const volumeH = Math.round((height - padding.top - padding.bottom) * 0.18);
  const priceH = height - padding.top - padding.bottom - volumeH - 8;
  const plotLeft = padding.left;
  const plotRight = width - padding.right;
  const plotW = plotRight - plotLeft;

  const highs = slice.map((c) => c.h);
  const lows = slice.map((c) => c.l);
  let max = Math.max(...highs);
  let min = Math.min(...lows);
  const pad = (max - min) * 0.08 || max * 0.001 || 1;
  max += pad;
  min -= pad;
  const range = max - min || 1;

  const yOf = (price) =>
    padding.top + priceH * (1 - (price - min) / range);

  const step = plotW / slice.length;
  const bodyW = Math.max(1, Math.min(11, step * 0.62));

  // ---- horizontal grid + price axis
  ctx.font = "10px Inter, sans-serif";
  ctx.textAlign = "left";
  ctx.textBaseline = "middle";
  for (let i = 0; i <= 4; i += 1) {
    const y = padding.top + (priceH * i) / 4;
    const price = max - (range * i) / 4;
    ctx.strokeStyle = "#eef0f2";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(plotLeft, Math.round(y) + 0.5);
    ctx.lineTo(plotRight, Math.round(y) + 0.5);
    ctx.stroke();

    ctx.fillStyle = "#9aa0a7";
    ctx.fillText(fmtPrice(price), plotRight + 8, y);
  }

  // ---- volume
  const volTop = padding.top + priceH + 8;
  const volMax = Math.max(...slice.map((c) => c.v), 1);
  slice.forEach((candle, index) => {
    const x = plotLeft + index * step + step / 2;
    const barH = (candle.v / volMax) * volumeH;
    ctx.fillStyle = candle.c >= candle.o ? "#cde9da" : "#f6d4d4";
    ctx.fillRect(x - bodyW / 2, volTop + volumeH - barH, bodyW, barH);
  });

  // ---- candles
  slice.forEach((candle, index) => {
    const x = plotLeft + index * step + step / 2;
    const up = candle.c >= candle.o;
    const color = up ? "#1a9c5b" : "#d94848";

    ctx.strokeStyle = color;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(Math.round(x) + 0.5, yOf(candle.h));
    ctx.lineTo(Math.round(x) + 0.5, yOf(candle.l));
    ctx.stroke();

    const openY = yOf(candle.o);
    const closeY = yOf(candle.c);
    const top = Math.min(openY, closeY);
    const bodyH = Math.max(1, Math.abs(closeY - openY));
    ctx.fillStyle = color;
    ctx.fillRect(x - bodyW / 2, top, bodyW, bodyH);
  });

  // ---- moving averages (7 / 25)
  drawMA(ctx, slice, step, plotLeft, yOf, 7, "#3f4a5c");
  drawMA(ctx, slice, step, plotLeft, yOf, 25, "#c9922b");

  // ---- last price line
  const last = slice[slice.length - 1];
  const lastY = yOf(last.c);
  ctx.strokeStyle = "#c8ced6";
  ctx.setLineDash([4, 3]);
  ctx.beginPath();
  ctx.moveTo(plotLeft, lastY);
  ctx.lineTo(plotRight, lastY);
  ctx.stroke();
  ctx.setLineDash([]);

  // ---- time axis
  ctx.fillStyle = "#9aa0a7";
  ctx.textAlign = "center";
  const ticks = Math.min(5, slice.length);
  for (let i = 0; i < ticks; i += 1) {
    const index = Math.floor((slice.length - 1) * (i / Math.max(1, ticks - 1)));
    const candle = slice[index];
    const x = plotLeft + index * step + step / 2;
    ctx.fillText(shortTime(candle.t), Math.min(plotRight - 20, Math.max(plotLeft + 20, x)), height - 8);
  }
}

function drawMA(ctx, slice, step, plotLeft, yOf, period, color) {
  if (slice.length < period) return;
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.2;
  ctx.beginPath();
  let started = false;
  for (let i = period - 1; i < slice.length; i += 1) {
    let sum = 0;
    for (let j = i - period + 1; j <= i; j += 1) sum += slice[j].c;
    const avg = sum / period;
    const x = plotLeft + i * step + step / 2;
    const y = yOf(avg);
    if (!started) {
      ctx.moveTo(x, y);
      started = true;
    } else {
      ctx.lineTo(x, y);
    }
  }
  ctx.stroke();
}

function shortTime(ms) {
  const date = new Date(ms);
  const mm = String(date.getMonth() + 1).padStart(2, "0");
  const dd = String(date.getDate()).padStart(2, "0");
  const hh = String(date.getHours()).padStart(2, "0");
  const mi = String(date.getMinutes()).padStart(2, "0");
  return state.timeframe === "1d" ? `${mm}/${dd}` : `${hh}:${mi}`;
}

/* ------------------------------------------------------------ ticket */
/**
 * Show the live price as a placeholder instead of writing it into the field.
 * A polling tick must never overwrite what the user is typing.
 */
function syncPricePlaceholder() {
  const input = $("priceInput");
  if (!input) return;
  const hint = state.lastPrice ? fmtPrice(state.lastPrice) : "0.00";
  input.placeholder = hint;
  if (state.orderType === "market") {
    input.disabled = true;
  } else {
    input.disabled = false;
  }
}

function referencePrice() {
  const typed = parseFloat($("priceInput").value);
  if (Number.isFinite(typed) && typed > 0) return typed;
  return state.lastPrice;
}

function updateEstimate() {
  const size = parseFloat($("sizeInput").value);
  const price = referencePrice();
  if (!Number.isFinite(size) || size <= 0 || !price) {
    $("estimateValue").textContent = "—";
    return;
  }
  if (state.sizeMode === "quote") {
    $("estimateValue").textContent = `${fmt(size / price, 6)} ${baseAsset()}`;
  } else {
    $("estimateValue").textContent = `${fmt(size * price, 2)} USDT`;
  }
}

function baseAsset() {
  const item = state.symbols.find((s) => s.symbol === state.symbol);
  return item?.base || state.symbol.split("/")[0];
}

function showFlash(message, tone) {
  const flash = $("ticketFlash");
  flash.hidden = false;
  flash.dataset.tone = tone;
  flash.textContent = message;
}

function hideFlash() {
  $("ticketFlash").hidden = true;
}

async function submitOrder(side) {
  const size = parseFloat($("sizeInput").value);
  const price = referencePrice();

  if (!Number.isFinite(size) || size <= 0) {
    showFlash("请填写有效的数量。", "err");
    return;
  }
  if (state.orderType === "limit" && (!Number.isFinite(price) || price <= 0)) {
    showFlash("限价单需要填写价格。", "err");
    return;
  }
  if (!state.symbol) {
    showFlash("请先选择交易对。", "err");
    return;
  }

  const buttons = [$("buyBtn"), $("sellBtn")];
  buttons.forEach((b) => (b.disabled = true));
  try {
    const result = await api("/api/orders", {
      method: "POST",
      body: JSON.stringify({
        market: state.market,
        symbol: state.symbol,
        side,
        order_type: state.orderType,
        size_mode: state.sizeMode,
        size,
        price: state.orderType === "limit" ? price : null,
      }),
    });
    const verb = side === "buy" ? "买入" : "卖出";
    const order = result.order || {};
    showFlash(
      `${verb}成功：${fmt(order.amount, 6)} ${baseAsset()} @ ${fmtPrice(
        order.average || order.price
      )}`,
      "ok"
    );
    $("sizeInput").value = "";
    updateEstimate();
    refreshAccount();
    if (!state.drawerOpen) {
      state.drawerOpen = true;
      $("drawer").dataset.open = "true";
      $("drawerToggle").setAttribute("aria-expanded", "true");
    }
    renderDrawer();
  } catch (error) {
    showFlash(error.message, "err");
  } finally {
    buttons.forEach((b) => (b.disabled = false));
  }
}

/* ------------------------------------------------------------ drawer */
function renderDrawer() {
  const host = $("drawerContent");
  const account = state.account;
  if (!account) {
    host.replaceChildren();
    return;
  }

  if (state.drawerTab === "open") {
    const rows = (account.orders || []).filter((o) => o.status === "open");
    host.replaceChildren(
      table(
        ["交易对", "方向", "类型", "数量", "价格", "时间"],
        rows.map((o) => [
          o.symbol,
          text(o.side === "buy" ? "买入" : "卖出", o.side === "buy" ? "up" : "down"),
          o.type === "limit" ? "限价" : "市价",
          fmt(o.amount, 6),
          o.price ? fmtPrice(o.price) : "市价",
          new Date(o.timestamp).toLocaleTimeString("zh-CN"),
        ]),
        "暂无未成交的委托"
      )
    );
    return;
  }

  if (state.drawerTab === "trades") {
    const rows = account.trades || [];
    host.replaceChildren(
      table(
        ["交易对", "方向", "价格", "数量", "手续费", "时间"],
        rows.map((t) => [
          t.symbol,
          text(t.side === "buy" ? "买入" : "卖出", t.side === "buy" ? "up" : "down"),
          fmtPrice(t.price),
          fmt(t.amount, 6),
          fmt(t.fee, 4),
          new Date(t.timestamp).toLocaleTimeString("zh-CN"),
        ]),
        "暂无成交"
      )
    );
    return;
  }

  const rows = account.positions || [];
  host.replaceChildren(
    table(
      ["交易对", "方向", "数量", "开仓价", "已实现盈亏"],
      rows.map((p) => [
        p.symbol,
        text(p.side === "long" ? "多头" : p.side === "short" ? "空头" : "—",
          p.side === "long" ? "up" : p.side === "short" ? "down" : ""),
        fmt(p.contracts, 6),
        fmtPrice(p.entry_price),
        text(fmt(p.realized_pnl, 2), p.realized_pnl >= 0 ? "up" : "down"),
      ]),
      "当前没有持仓"
    )
  );
}

function text(value, className) {
  const node = document.createElement("span");
  node.textContent = value;
  if (className) node.className = className;
  return node;
}

function table(headers, rows, emptyText) {
  if (!rows.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = emptyText;
    return empty;
  }
  const tableEl = document.createElement("table");
  tableEl.className = "table";

  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  headers.forEach((header, index) => {
    const th = document.createElement("th");
    th.textContent = header;
    if (index >= 3) th.className = "right";
    headRow.appendChild(th);
  });
  thead.appendChild(headRow);
  tableEl.appendChild(thead);

  const tbody = document.createElement("tbody");
  for (const row of rows) {
    const tr = document.createElement("tr");
    row.forEach((cell, index) => {
      const td = document.createElement("td");
      if (index >= 3) td.className = "right";
      if (cell instanceof Node) td.appendChild(cell);
      else td.textContent = String(cell);
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  }
  tableEl.appendChild(tbody);
  return tableEl;
}

/* -------------------------------------------------------------- start */
document.addEventListener("DOMContentLoaded", boot);
