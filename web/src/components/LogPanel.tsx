import { useCallback, useEffect, useState } from "react";
import { api } from "../api";

export interface LogItem {
  level: string;
  logger: string;
  message: string;
  detail: string;
  when: string;
  text: string;
}

interface LogPayload {
  revision: number;
  errors: number;
  warnings: number;
  items: LogItem[];
}

interface Props {
  open: boolean;
  onClose: () => void;
  onCounts: (errors: number, warnings: number) => void;
}

const EMPTY: LogPayload = { revision: 0, errors: 0, warnings: 0, items: [] };

/** Global error log. Lives outside every page so a failure raised while the
 * user is on another screen is still one click away. */
export default function LogPanel({ open, onClose, onCounts }: Props) {
  const [data, setData] = useState<LogPayload>(EMPTY);
  const [error, setError] = useState("");
  const [onlyError, setOnlyError] = useState(false);

  const load = useCallback(async () => {
    try {
      const result = await api<LogPayload>("/api/logs?limit=200");
      setData(result);
      setError("");
      onCounts(result.errors, result.warnings);
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : String(problem));
    }
  }, [onCounts]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(load, open ? 4000 : 15000);
    return () => window.clearInterval(timer);
  }, [load, open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  const items = onlyError
    ? data.items.filter((item) => item.level !== "WARNING")
    : data.items;

  return (
    <>
      <div className="log-mask" onClick={onClose} aria-hidden="true" />
      <aside className="log-panel" role="dialog" aria-label="报错日志">
        <div className="log-head">
          <strong>报错日志</strong>
          <span className="faint num">
            {data.errors} 错误 · {data.warnings} 警告
          </span>
          <span className="spacer" />
          <div className="segmented">
            <button type="button" aria-pressed={!onlyError} onClick={() => setOnlyError(false)}>
              全部
            </button>
            <button type="button" aria-pressed={onlyError} onClick={() => setOnlyError(true)}>
              仅错误
            </button>
          </div>
          <button type="button" className="icon-btn" aria-label="关闭" onClick={onClose}>
            <svg viewBox="0 0 16 16" width="16" height="16" fill="none" aria-hidden="true">
              <path
                d="m4 4 8 8M12 4l-8 8"
                stroke="currentColor"
                strokeWidth="1.4"
                strokeLinecap="round"
              />
            </svg>
          </button>
        </div>

        <div className="log-actions">
          <button type="button" className="btn-sm" onClick={() => void load()}>
            刷新
          </button>
          <button
            type="button"
            className="btn-sm"
            onClick={async () => {
              await api("/api/logs/clear", { method: "POST" });
              await load();
            }}
          >
            清空
          </button>
          <span className="spacer" />
          <span className="faint" style={{ fontSize: 11 }}>
            自动刷新
          </span>
        </div>

        {error && <div className="banner error">{error}</div>}

        <div className="log-body">
          {items.length === 0 && <div className="log-empty">暂无记录</div>}
          {items.map((item, index) => (
            <div
              key={`${item.when}-${index}`}
              className={`log-item ${item.level === "WARNING" ? "warn" : "err"}`}
            >
              <div className="log-meta">
                <span className="log-level">{item.level}</span>
                <span className="faint">{item.when}</span>
                <span className="spacer" />
                <span className="faint">{item.logger}</span>
              </div>
              <div className="log-message">{item.message}</div>
              {item.detail && <pre className="log-detail">{item.detail}</pre>}
            </div>
          ))}
        </div>
      </aside>
    </>
  );
}
