import { useState } from "react";
import { api, query } from "../api";
import { fmt, fmtPrice, parseNumber } from "../format";
import type { Account, PaperConditional, PaperOrder, PaperPosition } from "../types";
import NewsPanel from "./NewsPanel";
import Table from "./Table";

interface Props {
  account: Account | null;
  /** Pair whose headlines the news tab shows. */
  symbol?: string;
  /** Paper cancels/closes locally; live sends them to the exchange. */
  mode?: "paper" | "live";
  /** Reload the account after a cancel or close. */
  onChanged?: () => void;
  onError?: (message: string) => void;
}

type Tab = "open" | "trades" | "positions" | "news";

const TABS: [Tab, string][] = [
  ["open", "委托"],
  ["trades", "成交"],
  ["positions", "持仓"],
  ["news", "新闻"],
];

/** Fixed-height panel under the chart. Always visible: the tabs switch the
 * table, but the height never changes, so the chart above never jumps. */
export default function BottomTabs({
  account,
  symbol = "",
  mode = "paper",
  onChanged,
  onError,
}: Props) {
  const [tab, setTab] = useState<Tab>("open");
  const [collapsed, setCollapsed] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  // Per-position exit size in base coins; empty means "the whole position".
  const [closeSizes, setCloseSizes] = useState<Record<string, string>>({});

  const orders = (account?.orders ?? []).filter((order) => order.status === "open");
  const conditionals = (account?.conditional ?? []).filter(
    (item) => item.status === "open",
  );
  const trades = account?.trades ?? [];
  const positions = account?.positions ?? [];
  const count =
    tab === "open"
      ? orders.length + conditionals.length
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

  const cancel = (order: PaperOrder) =>
    run(`cancel:${order.id}`, () =>
      api(
        `/api/orders/${encodeURIComponent(order.id)}?${query({
          symbol: order.symbol,
          market: order.market,
          mode,
        })}`,
        { method: "DELETE" },
      ),
    );

  const cancelConditional = (item: PaperConditional) =>
    run(`cancel:${item.id}`, () =>
      api(`/api/orders/conditional/${encodeURIComponent(item.id)}`, {
        method: "DELETE",
      }),
    );

  const close = (position: PaperPosition) => {
    const typed = parseNumber(closeSizes[position.symbol] ?? "");
    const held = Math.abs(position.contracts);
    const size = typed > 0 ? Math.min(typed, held) : undefined;
    return run(`close:${position.symbol}`, async () => {
      await api("/api/positions/close", {
        method: "POST",
        body: JSON.stringify({
          symbol: position.symbol,
          size,
          mode,
          confirm_live: mode === "live",
        }),
      });
      setCloseSizes((current) => ({ ...current, [position.symbol]: "" }));
    });
  };

  return (
    <div className={`panel${collapsed ? " collapsed" : ""}`}>
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
            {key !== "news" && (
              <span className="count num" style={{ marginLeft: 6 }}>
                {key === "open"
                  ? orders.length + conditionals.length
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
                    body: JSON.stringify({
                      symbol: position.symbol,
                      mode,
                      confirm_live: mode === "live",
                    }),
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
            headers={["交易对", "方向", "类型", "数量", "触发/价格", "状态", "时间", "操作"]}
            rightFrom={3}
            rows={[
              ...conditionals.map((item) => [
                item.symbol,
                <span className={item.side === "buy" ? "up" : "down"}>
                  {item.side === "buy" ? "买入" : "卖出"}
                </span>,
                <span className="faint">
                  条件{item.type === "limit" ? "限价" : "市价"}
                </span>,
                fmt(item.amount, 6),
                <span>
                  {item.direction === "below" ? "跌破" : "涨破"}{" "}
                  <b className="num">{fmtPrice(item.trigger)}</b>
                </span>,
                "等待触发",
                new Date(item.createdAt).toLocaleTimeString("zh-CN", { hour12: false }),
                <button
                  type="button"
                  className="btn-sm"
                  disabled={busy !== null}
                  onClick={() => cancelConditional(item)}
                >
                  {busy === `cancel:${item.id}` ? "撤销中…" : "撤单"}
                </button>,
              ]),
              ...orders.map((order) => [
                order.symbol,
                <span className={order.side === "buy" ? "up" : "down"}>
                  {order.side === "buy" ? "买入" : "卖出"}
                </span>,
                order.type === "limit" ? "限价" : "市价",
                fmt(order.amount, 6),
                order.price ? fmtPrice(order.price) : "市价",
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
              ]),
            ]}
            empty="没有未成交的委托"
          />
        )}

        {tab === "trades" && (
          <Table
            headers={["交易对", "方向", "价格", "数量", "手续费", "时间"]}
            rightFrom={2}
            rows={trades.map((trade) => [
              trade.symbol,
              <span className={trade.side === "buy" ? "up" : "down"}>
                {trade.side === "buy" ? "买入" : "卖出"}
              </span>,
              fmtPrice(trade.price),
              fmt(trade.amount, 6),
              fmt(trade.fee, 4),
              new Date(trade.timestamp).toLocaleTimeString("zh-CN", { hour12: false }),
            ])}
            empty="暂无成交"
          />
        )}

        {tab === "positions" && (
          <Table
            headers={["交易对", "方向", "数量", "开仓价", "已实现盈亏", "平仓数量", "操作"]}
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
              fmtPrice(position.entry_price),
              <span className={position.realized_pnl >= 0 ? "up" : "down"}>
                {fmt(position.realized_pnl, 2)}
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
              </button>,
            ])}
            empty="当前没有持仓"
          />
        )}

        {tab === "news" && <NewsPanel symbol={symbol} />}
      </div>
    </div>
  );
}
