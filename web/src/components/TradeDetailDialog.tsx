import { fmt, fmtPrice } from "../format";
import type { TradeRow } from "../types";

interface Props {
  trade: TradeRow;
  onClose: () => void;
}

const MARKET_LABEL: Record<string, string> = {
  spot: "现货",
  usdm: "U 本位合约",
  coinm: "币本位合约",
};

/** Full record of one fill.
 *
 * The table can only show the essentials; an exit needs its fee, its booked
 * PnL and its order id to be checkable against the exchange, so a click opens
 * everything the account keeps for that fill.
 */
export default function TradeDetailDialog({ trade, onClose }: Props) {
  const when = new Date(trade.timestamp);
  const closed = trade.closedQty ?? 0;
  const realized = trade.realized ?? 0;
  return (
    <div
      className="modal-mask"
      role="presentation"
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        className="modal modal-wide"
        role="dialog"
        aria-modal="true"
        aria-label={`${trade.symbol} 成交详情`}
      >
        <div className="modal-title">成交详情 · {trade.symbol}</div>

        <div className="detail-grid">
          <span>方向</span>
          <b className={trade.side === "buy" ? "up" : "down"}>
            {trade.side === "buy" ? "买入" : "卖出"}
          </b>
          <span>订单类型</span>
          <b>{trade.type === "limit" ? "限价" : trade.type === "market" ? "市价" : (trade.type ?? "—")}</b>
          <span>市场</span>
          <b>{MARKET_LABEL[trade.market ?? ""] ?? trade.market ?? "—"}</b>
          <span>成交价</span>
          <b className="num">{fmtPrice(trade.price)}</b>
          <span>成交数量</span>
          <b className="num">{fmt(trade.amount, 8)}</b>
          <span>成交额</span>
          <b className="num">
            {trade.cost === undefined ? "—" : fmt(trade.cost, 2)}
          </b>
          <span>手续费</span>
          <b className="num down">{fmt(trade.fee, 6)}</b>
          <span>平仓数量</span>
          <b className="num">{closed > 0 ? fmt(closed, 8) : "—"}</b>
          <span>本笔已实现盈亏</span>
          <b className={`num ${realized >= 0 ? "up" : "down"}`}>
            {realized === 0 ? "—" : fmt(realized, 4)}
          </b>
          <span>成交时间</span>
          <b className="num">
            {when.toLocaleString("zh-CN", { hour12: false })}
          </b>
          <span>成交编号</span>
          <b className="num faint">{trade.id}</b>
          <span>委托编号</span>
          <b className="num faint">{trade.order ?? "—"}</b>
        </div>

        <p className="modal-text faint">
          本笔已实现盈亏 = 平掉那部分的价差 − 本笔手续费。
          只开仓的成交（平仓数量为 0）也会记一笔手续费，所以这里显示的是手续费本身。
        </p>

        <div className="modal-actions">
          <button type="button" className="btn ghost" onClick={onClose}>
            关闭
          </button>
        </div>
      </div>
    </div>
  );
}
