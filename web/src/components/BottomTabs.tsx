import { useEffect, useState, type PointerEvent as ReactPointerEvent } from "react";
import type { ReactNode } from "react";
import { api, query } from "../api";
import { fmt, fmtPrice, ledgerBasisNote, parseNumber } from "../format";
import type { Account, OrderRow, Position, Protection, TradeRow } from "../types";
import NewsPanel from "./NewsPanel";
import ProtectionDialog from "./ProtectionDialog";
import Table from "./Table";
import TradeDetailDialog from "./TradeDetailDialog";

/** 底部面板的默认高度：208 只能看见一行半，三级标题都在框外。 */
const PANEL_DEFAULT_HEIGHT = 320;
const PANEL_MIN_HEIGHT = 140;
/** 面板高度是界面偏好，不进 config.json：它不是交易参数，换台机器没必要跟着。 */
const PANEL_HEIGHT_KEY = "biTrader.bottomPanelHeight";

function storedPanelHeight(): number {
  const raw = window.localStorage.getItem(PANEL_HEIGHT_KEY);
  const value = Number(raw);
  if (!Number.isFinite(value) || value < PANEL_MIN_HEIGHT) return PANEL_DEFAULT_HEIGHT;
  return Math.round(value);
}

interface Props {
  account: Account | null;
  /** Pair whose headlines the news tab shows. */
  symbol?: string;
  /** Venue the panel is showing; closes must name it, or a futures exit is
   * looked up in the spot book and reported as "没有该交易对的持仓". */
  market?: string;
  /** mainnet or testnet: arming a guard on mainnet needs an acknowledgement. */
  network?: string;
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

/** 余额写进输入框时去掉多余的零和千分位：0.103000 就是 0.103，用户还能顺手改。 */
function plainAmount(value: number): string {
  return value > 0 ? String(Number(value.toFixed(8))) : "";
}

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
  network = "mainnet",
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
  /** 面板高度，拖拉改，双击回默认（见 panel-resizer）。图表高度不跟着变：
   *  它自己走 clamp()，面板变高只是让页面变长。 */
  const [panelHeight, setPanelHeight] = useState<number>(storedPanelHeight);
  useEffect(() => {
    window.localStorage.setItem(PANEL_HEIGHT_KEY, String(panelHeight));
  }, [panelHeight]);

  const startResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    const startY = event.clientY;
    const startHeight = panelHeight;
    const move = (moveEvent: PointerEvent) => {
      // 往上拖 = 面板变高；下限挡住折叠，上限留出图表那一屏。
      const next = startHeight - (moveEvent.clientY - startY);
      const ceiling = Math.max(PANEL_MIN_HEIGHT, Math.round(window.innerHeight * 0.8));
      setPanelHeight(Math.min(Math.max(next, PANEL_MIN_HEIGHT), ceiling));
    };
    const stop = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", stop);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop);
  };
  const [walletOpen, setWalletOpen] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  // Per-position exit size in base coins; empty means "the whole position".
  const [closeSizes, setCloseSizes] = useState<Record<string, string>>({});
  // Fill whose full record is open.
  const [tradeDetail, setTradeDetail] = useState<TradeRow | null>(null);
  // Position whose exchange-side guard is being edited.
  const [guarded, setGuarded] = useState<Position | null>(null);

  const orders = (account?.orders ?? []).filter((order) => order.status === "open");
  const trades = account?.trades ?? [];
  const positions = account?.positions ?? [];
  // 合约持仓都来自交易所，现货里要分「买来的」和「钱包本来就有的」。
  const ledgerPositions = positions.filter((item) => item.origin !== "wallet");
  const walletPositions = positions.filter((item) => item.origin === "wallet");
  const walletValue = walletPositions.reduce((sum, item) => sum + item.quote_value, 0);
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
        `/api/orders/${encodeURIComponent(order.id)}?${query({
          symbol: order.symbol,
          market: order.market,
        })}`,
        { method: "DELETE" },
      ),
    );

  /** 现货同一交易对会出两行（账本行 + 自有余额行），数量框和忙碌标记都得按「行」记。
   *  只按 symbol 记的话两行共用一个输入框：在钱包行填 1，账本行也跟着显示 1，
   *  点哪一行的按钮都按同一个数量走。 */
  const rowKey = (position: Position) => `${position.symbol}|${position.origin ?? "ledger"}`;

  /** 钱包余额的默认卖出量：整笔余额。买来的持仓留空，空值在 close() 里表示全平。
   *  现货的「持仓」就是钱包余额，不带 size 全平等于清空钱包，所以钱包行必须显式给数量。 */
  const defaultCloseSize = (position: Position) =>
    position.origin === "wallet" ? plainAmount(Math.abs(position.contracts)) : "";

  /** 数量框里该显示多少。钱包行是整笔余额；账本行只平账本那部分——contracts 是钱包事实，
   *  `1\` 个自己充的 ETH 混在里面的时候写 1.002500 会让人以为一次平掉全部。实测就是：占位
   *  写 1.002500，点「平仓」只卖掉 0.0025。 */
  const closeHint = (position: Position) =>
    Math.abs(
      position.origin === "wallet" ? position.contracts : position.ledger_size || position.contracts,
    );

  const close = (position: Position) => {
    const typed = parseNumber(closeSizes[rowKey(position)] ?? defaultCloseSize(position));
    const held = Math.abs(position.contracts);
    const size = typed > 0 ? Math.min(typed, held) : undefined;
    return run(`close:${rowKey(position)}`, async () => {
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
      // 成交后把这个交易对的临时数量删掉，而不是留一个空串。空串不是「没填过」：
      // 它会把钱包行的预填（整笔余额）顶掉，界面上变成一个空框，再点一次就变成
      // 「不带数量」——现货那等于让调用方自己给 size，后端直接 400。删掉键，
      // 下一帧又回到默认：钱包行给最新余额，账本行留空（空 = 全平）。
      setCloseSizes((current) => {
        const next = { ...current };
        delete next[rowKey(position)];
        return next;
      });
    });
  };

  /** 一行持仓。合约和现货共用，钱包余额那张表也用它。
   *  钱包余额不是持仓：没有开仓价、没有本地成交账本，卖它就是把这笔余额卖掉。
   *  所以钱包行按钮写「卖出」、数量框预填整笔余额；买来的持仓仍然是「平仓」。 */
  const positionRow = (position: Position) => {
    const wallet = position.origin === "wallet";
    const typedClose = parseNumber(closeSizes[rowKey(position)] ?? defaultCloseSize(position));
    const closing = busy === `close:${rowKey(position)}`;
    const actionLabel = wallet
      ? closing
        ? "卖出中…"
        : "卖出"
      : closing
        ? "平仓中…"
        : typedClose > 0
          ? "部分平仓"
          : "平仓";
    return [
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
      {/* 现货行的数量是钱包余额、盈亏只算账本那部分：不说明这个数看着像算错。 */}
      {ledgerBasisNote(position) && (
        <span className="faint" style={{ marginLeft: 4, fontSize: "var(--fs-12)" }}>
          {ledgerBasisNote(position)}
        </span>
      )}
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
      aria-label={`${wallet ? "卖出数量" : "平仓数量"} ${position.symbol}`}
      placeholder={fmt(closeHint(position), 6)}
      value={closeSizes[rowKey(position)] ?? defaultCloseSize(position)}
      onChange={(event) =>
        setCloseSizes((current) => ({
          ...current,
          [rowKey(position)]: event.target.value,
        }))
      }
    />,
    <div className="row-actions">
      {/* Spot has no venue-side stop, so the button would only
          lead to a rejection. Futures only. */}
      {market !== "spot" && (
        <button
          type="button"
          className="btn-sm"
          disabled={busy !== null}
          onClick={() => setGuarded(position)}
        >
          止损止盈
        </button>
      )}
      <button
        type="button"
        className="btn-sm"
        disabled={busy !== null}
        onClick={() => void close(position)}
      >
        {actionLabel}
      </button>
    </div>,
    ];
  };

  return (
    <>
      <div
        className={`panel${collapsed ? " collapsed" : ""}${tab === "ai" ? " ai-panel" : ""}`}
        style={collapsed ? undefined : { flexBasis: panelHeight }}
      >
        {/* 上沿的分隔条：上下拖动改高度，双击回默认。见 styles.css 的 .panel-resizer。 */}
        <div
          className="panel-resizer"
          role="separator"
          aria-orientation="horizontal"
          aria-label="调整面板高度"
          title="拖动调整高度，双击恢复默认"
          onPointerDown={startResize}
          onDoubleClick={() => setPanelHeight(PANEL_DEFAULT_HEIGHT)}
        />
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
          {tab === "positions" && ledgerPositions.length > 1 && (
            <button
              type="button"
              className="link-btn"
              style={{ marginRight: 12, alignSelf: "center" }}
              disabled={busy !== null}
              onClick={() =>
                run("close:all", async () => {
                  for (const position of ledgerPositions) {
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
            <span className="faint num" style={{ fontSize: "var(--fs-12)", alignSelf: "center" }}>
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
                // 算法单（止损/止盈/条件单）挂上就是等着触发，和限价单的「未成交」不是一回事。
                order.trigger ? "等待触发" : "未成交",
                // 算法单偶尔没有 createTime，压成 0 会显示成 1970 年 08:00。
                order.timestamp
                  ? new Date(order.timestamp).toLocaleTimeString("zh-CN", { hour12: false })
                  : "—",
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
            <>
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
                rows={ledgerPositions.map(positionRow)}
                empty={
                  walletPositions.length ? "没有买来的持仓，钱包里的余额在下面" : "当前没有持仓"
                }
              />
              {/* 钱包里没买过的那部分单独收起来：测试网白送几十个币，展开就把
                  真正的持仓挤没了。 */}
              {walletPositions.length > 0 && (
                <div className="wallet-group">
                  <button
                    type="button"
                    className="wallet-toggle"
                    aria-expanded={walletOpen}
                    onClick={() => setWalletOpen((open) => !open)}
                  >
                    <span>
                      另有 {walletPositions.length} 项钱包余额 · 合计{" "}
                      {fmt(walletValue, 2)} {account?.quote_asset ?? "USDT"}
                    </span>
                    <em>非成交持仓：充值、空投或测试网赠送，没有成本价</em>
                    <b className="faint">{walletOpen ? "收起" : "展开"}</b>
                  </button>
                  {walletOpen && (
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
                "卖出数量",
                "操作",
              ]}
              rightFrom={2}
                      rows={walletPositions.map(positionRow)}
                      empty=""
                    />
                  )}
                </div>
              )}
            </>
          )}

          {tab === "ai" && aiPanel}
          {tab === "news" && <NewsPanel symbol={symbol} />}
        </div>
      </div>

      {tradeDetail && (
        <TradeDetailDialog trade={tradeDetail} onClose={() => setTradeDetail(null)} />
      )}

      {guarded && (
        <ProtectionDialog
          position={guarded}
          protection={guardFor(guarded.symbol)}
          market={market}
          network={network}
          onClose={() => setGuarded(null)}
          onSaved={onChanged}
        />
      )}
    </>
  );
}
