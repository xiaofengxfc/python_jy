import { useState } from "react";
import { fmt, fmtPrice } from "../format";
import type { Account } from "../types";
import Table from "./Table";

interface Props {
  account: Account | null;
}

type Tab = "open" | "trades" | "positions";

const TABS: [Tab, string][] = [
  ["open", "委托"],
  ["trades", "成交"],
  ["positions", "持仓"],
];

/** Fixed-height panel under the chart. Always visible: the tabs switch the
 * table, but the height never changes, so the chart above never jumps. */
export default function BottomTabs({ account }: Props) {
  const [tab, setTab] = useState<Tab>("open");
  const [collapsed, setCollapsed] = useState(false);

  const orders = (account?.orders ?? []).filter((order) => order.status === "open");
  const trades = account?.trades ?? [];
  const positions = account?.positions ?? [];
  const count =
    tab === "open" ? orders.length : tab === "trades" ? trades.length : positions.length;

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
            <span className="count num" style={{ marginLeft: 6 }}>
              {key === "open"
                ? orders.length
                : key === "trades"
                  ? trades.length
                  : positions.length}
            </span>
          </button>
        ))}
        <span className="spacer" />
        <span className="faint num" style={{ fontSize: 11, alignSelf: "center" }}>
          {count} 条
        </span>
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
            headers={["交易对", "方向", "类型", "数量", "价格", "状态", "时间"]}
            rightFrom={3}
            rows={orders.map((order) => [
              order.symbol,
              <span className={order.side === "buy" ? "up" : "down"}>
                {order.side === "buy" ? "买入" : "卖出"}
              </span>,
              order.type === "limit" ? "限价" : "市价",
              fmt(order.amount, 6),
              order.price ? fmtPrice(order.price) : "市价",
              order.status === "open" ? "未成交" : order.status,
              new Date(order.timestamp).toLocaleTimeString("zh-CN", { hour12: false }),
            ])}
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
            headers={["交易对", "方向", "数量", "开仓价", "已实现盈亏"]}
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
            ])}
            empty="当前没有持仓"
          />
        )}
      </div>
    </div>
  );
}
