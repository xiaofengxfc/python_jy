import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { fmt, fmtPrice } from "../format";
import type { Portfolio } from "../types";
import LineChart from "../components/LineChart";
import Table from "../components/Table";

interface Props {
  onEquity: (value: number) => void;
}

type Ledger = "trades" | "orders";

export default function PortfolioPage({ onEquity }: Props) {
  const [data, setData] = useState<Portfolio | null>(null);
  const [error, setError] = useState("");
  const [ledger, setLedger] = useState<Ledger>("trades");

  const load = useCallback(async () => {
    try {
      const next = await api<Portfolio>("/api/portfolio");
      setData(next);
      setError("");
      onEquity(next.equity);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }, [onEquity]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(load, 5000);
    return () => window.clearInterval(timer);
  }, [load]);

  const positions = data?.positions ?? [];
  const orders = data?.orders ?? [];
  const trades = data?.trades ?? [];
  const quote = data?.quote_asset ?? "USDT";

  const unrealized = useMemo(
    () => positions.reduce((sum, position) => sum + position.unrealized, 0),
    [positions],
  );
  const holdings = useMemo(
    () => positions.reduce((sum, position) => sum + Math.abs(position.quote_value), 0),
    [positions],
  );

  const equitySeries = useMemo(() => {
    const points = (data?.equity_history ?? []).map((point) => ({
      x: point.t,
      y: point.equity,
    }));
    return [{ name: "账户权益", color: "#2563eb", points }];
  }, [data?.equity_history]);

  // Allocation is by absolute position value so shorts are visible too.
  const allocation = useMemo(() => {
    const parts = positions.map((position) => ({
      name: position.symbol,
      value: Math.abs(position.quote_value),
      negative: position.contracts < 0,
    }));
    if (data && data.cash > 0) parts.push({ name: "可用现金", value: data.cash, negative: false });
    const total = parts.reduce((sum, part) => sum + part.value, 0) || 1;
    return parts
      .map((part) => ({ ...part, pct: (part.value / total) * 100 }))
      .sort((a, b) => b.value - a.value)
      .slice(0, 7);
  }, [positions, data]);

  const returnPct = data && data.starting_balance > 0
    ? ((data.equity - data.starting_balance) / data.starting_balance) * 100
    : 0;
  const up = (data?.equity ?? 0) >= (data?.starting_balance ?? 0);

  return (
    <>
      <div className="page-head">
        <h1 className="page-title">资产</h1>
        <span className="page-sub">交易所账户 · 每 5 秒刷新</span>
        <span className="spacer" />
      </div>

      {error && <div className="banner error">{error}</div>}

      <div className="stack">
        <div className="metrics">
          <div className="metric">
            <span className="metric-label">总权益</span>
            <span className="metric-value num">
              {fmt(data?.equity ?? 0, 2)}
              <span className="faint" style={{ fontSize: 11, marginLeft: 4 }}>{quote}</span>
            </span>
          </div>
          <div className="metric">
            <span className="metric-label">可用余额</span>
            <span className="metric-value num">{fmt(data?.cash ?? 0, 2)}</span>
          </div>
          <div className="metric">
            <span className="metric-label">持仓市值</span>
            <span className="metric-value num">{fmt(holdings, 2)}</span>
          </div>
          <div className="metric">
            <span className="metric-label">浮动盈亏</span>
            <span className={`metric-value num ${unrealized >= 0 ? "up" : "down"}`}>
              {fmt(unrealized, 2)}
            </span>
          </div>
          <div className="metric">
            <span className="metric-label">已实现盈亏</span>
            <span className={`metric-value num ${(data?.realized_pnl ?? 0) >= 0 ? "up" : "down"}`}>
              {fmt(data?.realized_pnl ?? 0, 2)}
            </span>
          </div>
          <div className="metric">
            <span className="metric-label">累计收益率</span>
            <span className={`metric-value num ${up ? "up" : "down"}`}>
              {returnPct >= 0 ? "+" : ""}
              {returnPct.toFixed(2)}%
            </span>
          </div>
        </div>

        <div className="stack">
          <div className="card">
            <div className="card-head">
              <span className="card-title">权益曲线</span>
              <span className="spacer" />
              <span className="faint" style={{ fontSize: 11 }}>
                初始 {fmt(data?.starting_balance ?? 0, 2)}
              </span>
            </div>
            <div className="card-body">
              <LineChart
                series={equitySeries}
                height={230}
                baseline={data?.starting_balance}
                valueFormat={(value) => fmt(value, 0)}
                empty="成交后开始记录权益曲线"
              />
            </div>
          </div>

          <div className="card">
            <div className="card-head">
              <span className="card-title">资产分布</span>
            </div>
            <div className="card-body">
              {allocation.length ? (
                <div className="allocation">
                  {allocation.map((part) => (
                    <div key={part.name} className="alloc-row">
                      <span className={part.negative ? "down" : ""}>{part.name}</span>
                      <div className="alloc-bar">
                        <div className="alloc-fill" style={{ width: `${part.pct}%` }} />
                      </div>
                      <span className="alloc-value">{part.pct.toFixed(1)}%</span>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="empty">暂无持仓</div>
              )}
            </div>
          </div>
        </div>

        <div className="card">
          <div className="card-head">
            <span className="card-title">当前持仓</span>
            <span className="spacer" />
            <span className="faint" style={{ fontSize: 11 }}>{positions.length} 个</span>
          </div>
          <Table
            headers={["交易对", "方向", "数量", "开仓价", "最新价", "浮动盈亏", "市值"]}
            rightFrom={2}
            rows={positions.map((position) => [
              position.symbol,
              <span className={position.contracts > 0 ? "up" : "down"}>
                {position.contracts > 0 ? "多头" : "空头"}
              </span>,
              fmt(Math.abs(position.contracts), 6),
              position.entry_price > 0 ? fmtPrice(position.entry_price) : "—",
              fmtPrice(position.last),
              <span className={position.unrealized >= 0 ? "up" : "down"}>
                {fmt(position.unrealized, 2)}
              </span>,
              fmt(Math.abs(position.quote_value), 2),
            ])}
            empty="当前没有持仓"
          />
        </div>

        <div className="card">
          <div className="card-head">
            <span className="card-title">明细</span>
            <span className="spacer" />
            <div className="segmented">
              <button
                type="button"
                aria-pressed={ledger === "trades"}
                onClick={() => setLedger("trades")}
              >
                成交 {trades.length}
              </button>
              <button
                type="button"
                aria-pressed={ledger === "orders"}
                onClick={() => setLedger("orders")}
              >
                全部委托 {orders.length}
              </button>
            </div>
          </div>
          {ledger === "trades" ? (
            <Table
              headers={["交易对", "方向", "价格", "数量", "成交额", "手续费", "时间"]}
              rightFrom={2}
              rows={trades.map((trade) => [
                trade.symbol,
                <span className={trade.side === "buy" ? "up" : "down"}>
                  {trade.side === "buy" ? "买入" : "卖出"}
                </span>,
                fmtPrice(trade.price),
                fmt(trade.amount, 6),
                fmt(trade.price * trade.amount, 2),
                fmt(trade.fee, 4),
                new Date(trade.timestamp).toLocaleString("zh-CN", { hour12: false }),
              ])}
              empty="还没有成交记录"
            />
          ) : (
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
                <StatusLabel status={order.status} />,
                new Date(order.timestamp).toLocaleString("zh-CN", { hour12: false }),
              ])}
              empty="还没有委托记录"
            />
          )}
        </div>
      </div>
    </>
  );
}

function StatusLabel({ status }: { status: string }) {
  const map: Record<string, [string, string]> = {
    open: ["未成交", "dim"],
    closed: ["已成交", "up"],
    canceled: ["已撤销", "faint"],
    rejected: ["已拒绝", "down"],
  };
  const [text, className] = map[status] ?? [status, "dim"];
  return <span className={className}>{text}</span>;
}
