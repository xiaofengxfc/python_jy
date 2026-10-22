import { useState } from "react";
import type { ReactNode } from "react";
import { api, query } from "../api";
import { fmt, fmtPrice, parseNumber } from "../format";
import type { Account, OrderRow, Position, Protection, TradeRow } from "../types";
import NewsPanel from "./NewsPanel";
import Table from "./Table";
import TradeDetailDialog from "./TradeDetailDialog";

interface Props {
  account: Account | null;
  /** Pair whose headlines the news tab shows. */
  symbol?: string;
  /** Venue the panel is showing; closes must name it, or a futures exit is
   * looked up in the spot book and reported as "没有该交易对的持仓". */
  market?: string;
  /** Reload the account after a cancel or close. */
  onChanged?: () => void;
  onError?: (message: string) => void;
  /** The AI panel is owned by the trading page (it needs chart levels and the
   * current timeframe), but it is rendered here so the analysis is inline and
   * never floats over the chart. */
  aiPanel?: ReactNode;
  /** Optional controlled tab, so a chart-toolbar button can jump to AI 分析. */
  activeTab?: BottomTab;
  onTabChange?: (tab: BottomTab) => void;
}

export type BottomTab = "open" | "trades" | "positions" | "ai" | "news";

const TABS: [BottomTab, string][] = [
  ["open", "委托"],
  ["trades", "成交"],
  ["positions", "持仓"],
  ["ai", "AI 分析"],
  ["news", "新闻"],
];

/** Fixed-height panel under the chart. Always visible: the tabs switch the
 * table, but the height never changes, so the chart above never jumps. */
export default function BottomTabs({
  account,
  symbol = "",
  market = "spot",
  onChanged,
  onError,
  aiPanel,
  activeTab,
  onTabChange,
}: Props) {
  const [internalTab, setInternalTab] = useState<BottomTab>("open");
  const tab = activeTab ?? internalTab;
  const setTab = (next: BottomTab) => {
    setInternalTab(next);
    onTabChange?.(next);
  };
  const [collapsed, setCollapsed] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  // Per-position exit size in base coins; empty means "the whole position".
  const [closeSizes, setCloseSizes] = useState<Record<string, string>>({});
  // Fill whose full record is open.
  const [tradeDetail, setTradeDetail] = useState<TradeRow | null>(null);

  const orders = (account?.orders ?? []).filter((order) => order.status === "open");
  const trades = account?.trades ?? [];
  const positions = account?.positions ?? [];
  const guards = account?.protections ?? [];
  /** The stop / take-profit orders the venue holds for a pair, if any. */
  const guardFor = (pair: string): Protection | null =>
    guards.find((guard) => guard.symbol === pair) ?? null;
  const count =
    tab === "open"
      ? orders.length
      : tab === "trades"
        ? trades.length
        : tab === "positions"
          ? positions.length
          : null;

  const run = async (key: string, action: () => Promise<unknown>) => {
    if (busy) return;
    setBusy(key);
    try {
      await action();
      onChanged?.();
    } catch (error) {
      onError?.(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(null);
    }
  };

  const cancel = (order: OrderRow) =>
    run(`cancel:${order.id}`, () =>
      api(
        `T/api/orders/${encodeURIComponent(order.id)}?${query({
          symbol: order.symbol,
          market: order.market,
        })}`,
        { method: "DELETE" },
      ),
    );

  const close = (position: Position) => {
    const typed = parseNumber(closeSizes[position.symbol] ?? "");
    const held = Math.abs(position.contracts);
    const size = typed > 0 ? Math.min(typed, held) : undefined;
    return run(`close:${position.symbol}`, async () => {
      await api("/api/positions/close", {
        method: "POST",
        body: JSON.stringify({
          symbol: position.symbol,
          // The venue must be named: the server would otherwise infer it and a
          // futures exit could be looked up in the spot book.
          market,
          size,
        }),
      });
      setCloseSizes((current) => ({ ...current, [position.symbol]: "" }));
    });
  };

  return (
    <>
      <div
        className={`panel${collapsed ? " collapsed" : ""}${tab === "ai" ? " ai-panel" : ""}`}
      >
        <div className="tabs" role="tablist">
          {TABS.map(([key, label]) => (
            <button
              key={key}
              type="button"
              role="tab"
              className="tab"
              aria-selected={tab === key}
              onClick={() => setTab(key)}
            >
              {label}
              {key !== "news" && key !== "ai" && (
                <span className="count num" style={{ marginLeft: 6 }}>
                  {key === "open"
                    ? orders.length
                    : key === "trades"
                      ? trades.length
                      : positions.length}
                </span>
              )}
            </button>
          ))}
          <span className="spacer" />
          {tab === "positions" && positions.length > 1 && (
            <button
              type="button"
              className="link-btn"
              style={{ marginRight: 12, alignSelf: "center" }}
              disabled={busy !== null}
              onClick={() =>
                run("close:all", async () => {
                  for (const position of positions) {
                    await api("/api/positions/close", {
                      method: "POST",
                      body: JSON.stringify({ symbol: position.symbol, market }),
                    });
                  }
                })
              }
            >
              全部平仓
            </button>
          )}
          {count !== null && (
            <span className="faint num" style={{ fontSize: 11, alignSelf: "center" }}>
              {count} 条
            </span>
          )}
          <button
            type="button"
            className="panel-toggle"
            aria-label={collapsed ? "展开委托面板" : "收起委托面板"}
            aria-expanded={!collapsed}
            onClick={() => setCollapsed((value) => !value)}
          >
            <svg
              viewBox="0 0 16 16"
              width="14"
              height="14"
              fill="none"
              style={{ transform: collapsed ? "rotate(180deg)" : undefined }}
            >
              <path
                d="m4 10 4-4 4 4"
                stroke="currentColor"
                strokeWidth="1.5"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </button>
        </div>

        <div className="panel-body" hidden={collapsed}>
          {tab === "open" && (
            <Table
              headers={["交易对", "方向", "类型", "数量", "价格", "状态", "时间", "操作"]}
              rightFrom={3}
              rows={orders.map((order) => [
                order.symbol,
                <span className={order.side === "buy" ? "up" : "down"}>
                  {order.side === "buy" ? "买入" : "卖出"}
                </span>,
                order.trigger
                  ? `止损/止盈`
                  : order.type === "limit"
                    ? "限价"
                    : "市价",
                fmt(order.amount, 6),
                order.price ? fmtPrice(order.price) : order.trigger ? fmtPrice(order.trigger) : "市价",
                "未成交",
                new Date(order.timestamp).toLocaleTimeString("zh-CN", { hour12: false }),
                <button
                  type="button"
                  className="btn-sm"
                  disabled={busy !== null}
                  onClick={() => cancel(order)}
                >
                  {busy === `cancel:${order.id}` ? "撤销中…" : "撤单"}
                </button>,
              ])}
              empty="没有未成交的委托"
            />
          )}

          {tab === "trades" && (
            <Table
              headers={["交易对", "方向", "价格", "数量", "成交额", "手续费", "已实现盈亏", "时间"]}
              rightFrom={2}
              rows={trades.map((trade) => [
                trade.symbol,
                <span className={trade.side === "buy" ? "up" : "down"}>
                  {trade.side === "buy" ? "买入" : "卖出"}
                </span>,
                fmtPrice(trade.price),
                fmt(trade.amount, 6),
                trade.cost === undefined ? "—" : fmt(trade.cost, 2),
                <span className="down">{fmt(trade.fee, 4)}</span>,
                <span className={(trade.realized ?? 0) >= 0 ? "up" : "down"}>
                  {trade.realized ? fmt(trade.realized, 2) : "—"}
                </span>,
                new Date(trade.timestamp).toLocaleTimeString("zh-CN", { hour12: false }),
              ])}
              // A row is a fill; clicking it opens everything the account keeps
              // about that fill (fees, booked PnL, both ids, full timestamp).
              onRowClick={(index) => setTradeDetail(trades[index] ?? null)}
              empty="暂无成交"
            />
          )}

          {tab === "positions" && (
            <Table
              headers={[
                "交易对",
                "方向",
                "数量",
                "开仓价",
                "最新价",
                "浮动盈亏",
                "已实现盈亏",
                "交易所止损 / 止盈",
                "平仓数量",
                "操作",
              ]}
              rightFrom={2}
              rows={positions.map((position) => [
                position.symbol,
                <span
                  className={
                    position.side === "long" ? "up" : position.side === "short" ? "down" : ""
                  }
                >
                  {position.side === "long" ? "多头" : position.side === "short" ? "空头" : "—"}
                </span>,
                fmt(position.contracts, 6),
                // No fill history for this coin (a transfer, or a position
                // opened elsewhere) means the cost basis is unknown: say so
                // instead of printing a fake zero.
                position.entry_price > 0 ? fmtPrice(position.entry_price) : "—",
                fmtPrice(position.last),
                // Marked by the same server call the 资产 page uses, so the two
                // pages cannot show a different PnL for one position.
                <span className={position.unrealized >= 0 ? "up" : "down"}>
                  {fmt(position.unrealized, 2)}
                </span>,
                <span className={position.realized_pnl >= 0 ? "up" : "down"}>
                  {fmt(position.realized_pnl, 2)}
                </span>,
                // The protective orders the venue holds, read straight from the
                // exchange: the app keeps no local copy that could go stale.
                <span className="num">
                  {(() => {
                    const guard = guardFor(position.symbol);
                    if (!guard || (guard.stop_loss <= 0 && guard.take_profit <= 0)) {
                      return <span className="faint">无</span>;
                    }
                    return (
                      <>
                        <b className="down">
                          {guard.stop_loss > 0 ? fmtPrice(guard.stop_loss) : "—"}
                        </b>
                        {" / "}
                        <b className="up">
                          {guard.take_profit > 0 ? fmtPrice(guard.take_profit) : "—"}
                        </b>
                      </>
                    );
                  })()}
                </span>,
                <input
                  className="input input-xs"
                  type="text"
                  inputMode="decimal"
                  autoComplete="off"
                  aria-label={`平仓数量 ${position.symbol}`}
                  placeholder={fmt(Math.abs(position.contracts), 6)}
                  value={closeSizes[position.symbol] ?? ""}
                  onChange={(event) =>
                    setCloseSizes((current) => ({
                      ...current,
                      [position.symbol]: event.target.value,
                    }))
                  }
                />,
                <div className="row-actions">
                  <button
                    type="button"
                    className="btn-sm"
                    disabled={busy !== null}
                    onClick={() => void close(position)}
                  >
                    {busy === `close:${position.symbol}`
                      ? "平仓中…"
                      : parseNumber(closeSizes[position.symbol] ?? "") > 0
                        ? "部分平仓"
                        : "平仓"}
                  </button>
                </div>,
              ])}
              empty="当前没有持仓"
            />
          )}

          {tab === "ai" && aiPanel}
          {tab === "news" && <NewsPanel symbol={symbol} />}
        </div>
      </div>

      {tradeDetail && (
        <TradeDetailDialog trade={tradeDetail} onClose={() => setTradeDetail(null)} />
      )}
    </>
  );
}
