import { useCallback, useEffect, useState } from "react";
import { api, query } from "../api";
import type { NewsFeed } from "../types";

interface Props {
  symbol: string;
}

/** Feed refresh period. The server caches for two minutes, so polling faster
 * than that only moves bytes, not information. */
const REFRESH_MS = 120_000;

function when(iso: string): string {
  if (!iso) return "";
  const stamp = Date.parse(iso);
  if (Number.isNaN(stamp)) return "";
  const minutes = Math.max(0, Math.round((Date.now() - stamp) / 60_000));
  if (minutes < 60) return `${minutes} 分钟前`;
  if (minutes < 60 * 24) return `${Math.round(minutes / 60)} 小时前`;
  return `${Math.round(minutes / (60 * 24))} 天前`;
}

/** CryptoPanic headlines for the pair being traded.
 *
 * A missing token and an upstream failure are both rendered in place: an empty
 * list would otherwise read as "nothing is happening", which is a different
 * claim from "the feed could not be read". */
export default function NewsPanel({ symbol }: Props) {
  const [feed, setFeed] = useState<NewsFeed | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    if (!symbol) return;
    setLoading(true);
    try {
      setFeed(await api<NewsFeed>(`/api/news?${query({ symbol, limit: 20 })}`));
    } catch (error) {
      setFeed({
        configured: true,
        items: [],
        cached: false,
        error: error instanceof Error ? error.message : String(error),
        source: "CryptoPanic",
      });
    } finally {
      setLoading(false);
    }
  }, [symbol]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  if (feed && !feed.configured) {
    return (
      <div className="news-empty">
        <span>未配置 CryptoPanic Token，新闻暂不可用。</span>
        <span className="faint">在「设置 → 新闻来源」填入免费 Token 后即可显示。</span>
      </div>
    );
  }

  return (
    <div className="news-wrap">
      {feed?.error && <div className="banner error">{feed.error}</div>}
      {feed && feed.items.length > 0 ? (
        <ul className="news-list">
          {feed.items.map((item) => (
            <li key={item.id} className="news-item">
              <a href={item.url} target="_blank" rel="noreferrer" className="news-title">
                {item.title}
              </a>
              <div className="news-meta">
                {item.source && <span>{item.source}</span>}
                {item.published_at && <span>{when(item.published_at)}</span>}
                {item.currencies.slice(0, 3).map((code) => (
                  <span key={code} className="news-tag">
                    {code}
                  </span>
                ))}
                {item.important > 0 && <span className="news-tag hot">重要</span>}
              </div>
            </li>
          ))}
        </ul>
      ) : (
        <div className="news-empty">
          {loading ? "正在加载新闻…" : feed ? "暂无该交易对的相关新闻。" : "正在加载新闻…"}
        </div>
      )}
      <div className="news-foot faint">
        来源 CryptoPanic · 每 2 分钟刷新
        {feed?.cached ? " · 当前为缓存结果" : ""}
      </div>
    </div>
  );
}
