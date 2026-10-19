import type { Book } from "../types";
import { fmt, fmtPrice } from "../format";

interface Props {
  book: Book;
  onPickPrice: (price: number) => void;
}

/** Ask rows are rendered high→low so the spread sits in the middle, the way
 * every exchange presents a ladder. Each row is a button: clicking it drops
 * that price into the limit field instead of making the user copy it. */
export default function OrderBook({ book, onPickPrice }: Props) {
  const bids = book.bids.slice(0, 6);
  const asks = book.asks.slice(0, 6).slice().reverse();
  const maxQty = Math.max(
    ...bids.map(([, qty]) => qty),
    ...asks.map(([, qty]) => qty),
    0.000001,
  );

  const spread =
    book.bids[0] && book.asks[0] ? book.asks[0][0] - book.bids[0][0] : 0;

  const row = ([price, qty]: [number, number], side: "bid" | "ask") => (
    <button
      key={`${side}-${price}`}
      type="button"
      className="book-row"
      onClick={() => onPickPrice(price)}
      title={`填入限价 ${fmtPrice(price)}`}
    >
      <span className={`book-bar ${side}`} style={{ width: `${(qty / maxQty) * 100}%` }} />
      <span className={`book-price ${side === "bid" ? "up" : "down"}`}>
        {fmtPrice(price)}
      </span>
      <span className="book-qty">{fmt(qty, 4)}</span>
    </button>
  );

  return (
    <div className="book">
      <div className="book-side">
        <div className="book-head">
          <span>卖盘</span>
          <span>数量</span>
        </div>
        {asks.map((entry) => row(entry, "ask"))}
      </div>
      <div className="spread">
        <span>价差</span>
        <span className="num">{spread > 0 ? fmtPrice(spread) : "—"}</span>
      </div>
      <div className="book-side">
        <div className="book-head">
          <span>买盘</span>
          <span>数量</span>
        </div>
        {bids.map((entry) => row(entry, "bid"))}
      </div>
    </div>
  );
}
