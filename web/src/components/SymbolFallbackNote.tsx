import { MARKET_LABELS } from "../format";

/** 一句话说明「你要的那一对在这个市场没有，换成了这一对」。
 *
 *  设置里的默认交易对、或者页面上原来选的那一对，切到另一个市场时写法就不一样了
 *  （现货 ETH/USDT、合约 ETH/USDT:USDT），列表里没有那一项就只能退到别的对。
 *  退回去这件事必须说出来，否则用户会以为自己选的对被吞了。三处（交易页行情头、
 *  回测页配置、设置页默认交易对）共用这一行，句子和样式都不各写一套。
 */

/** 句子里用全称比 format.MARKET_LABELS 里的简称（「U 本位」）好读。 */
const FULL_MARKET_LABELS: Record<string, string> = {
  spot: "现货",
  usdm: "U 本位合约",
  coinm: "币本位合约",
  delivery: "交割合约",
};

interface Props {
  /** 原本要用的那一对。空的话什么都不渲染。 */
  from: string;
  /** 实际换成的。 */
  to: string;
  /** 当前市场的 value（spot / usdm / coinm）。 */
  market: string;
  /** strip：行情头下面那行（带左右内边距）；form：表单里贴着字段，去掉内边距。 */
  layout?: "strip" | "form";
}

export default function SymbolFallbackNote({ from, to, market, layout = "strip" }: Props) {
  // 没得可退、或者退回了同一对，就别占地方。
  if (!from || !to || from === to) return null;
  const label = FULL_MARKET_LABELS[market] ?? MARKET_LABELS[market] ?? market;
  return (
    <div className={layout === "form" ? "symbol-note faint inline" : "symbol-note faint"}>
      {from} 在「{label}」市场不可用，已切到 {to}
    </div>
  );
}
