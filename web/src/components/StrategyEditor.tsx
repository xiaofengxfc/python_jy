import { useCallback, useEffect, useMemo, useState } from "react";
import CodeMirror from "@uiw/react-codemirror";
import { python } from "@codemirror/lang-python";
import { api } from "../api";
import type { Strategy } from "../types";

/** Mirrors `btrader.strategy.source.DEFAULT_TEMPLATE`; kept as a string so the
 * editor can create a plugin without a round trip. */
const TEMPLATE = `"""A minimal biTrader strategy.

\`generate\` returns a DataFrame with a \`target\` column:
  1.0 = long, 0.0 = flat, -1.0 = short.
Signals are evaluated on closed candles by the auto trader and the backtester.
"""

import pandas as pd

from btrader.strategy.base import IntParam, Strategy


class MyStrategy(Strategy):
    key = "my_strategy"
    name = "我的策略"
    description = "收盘价上穿均线做多，下穿离场。"
    params_spec = [IntParam("period", "均线周期", 20, 2, 200)]

    def generate(self, df: pd.DataFrame, params: dict) -> pd.DataFrame:
        ma = df["close"].rolling(int(params["period"])).mean()
        target = (df["close"] > ma).astype(float)
        return pd.DataFrame({"target": target}, index=df.index)
`;

interface SourceDetail {
  key: string;
  name: string;
  code: string;
  filename: string;
  editable: boolean;
  builtin: boolean;
}

interface Issue {
  code?: string;
  message?: string;
  line?: number;
}

interface ValidationReport {
  ok: boolean;
  key?: string;
  issues?: Issue[];
  imports?: string[];
}

interface Props {
  strategies: Strategy[];
  /** Currently selected strategy key in the page. */
  value: string;
  onSelect: (key: string) => void;
  onSaved?: (key: string) => void;
  onClose?: () => void;
}

/** Inline strategy source editor: read, validate, save and hot-reload a user
 * plugin. Built-ins are read-only and can be copied into a new plugin. */
export default function StrategyEditor({
  strategies,
  value,
  onSelect,
  onSaved,
  onClose,
}: Props) {
  const [selected, setSelected] = useState(value);
  const [detail, setDetail] = useState<SourceDetail | null>(null);
  const [code, setCode] = useState("");
  const [original, setOriginal] = useState("");
  const [isNew, setIsNew] = useState(false);
  const [newKey, setNewKey] = useState("my_strategy");
  const [busy, setBusy] = useState<"" | "load" | "save" | "validate">("");
  const [renaming, setRenaming] = useState(false);
  const [renameKey, setRenameKey] = useState("");
  const [renameName, setRenameName] = useState("");
  const [renameBusy, setRenameBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [report, setReport] = useState<ValidationReport | null>(null);

  const selectedMeta = useMemo(
    () => strategies.find((item) => item.key === selected) ?? null,
    [strategies, selected],
  );

  const load = useCallback(
    async (key: string) => {
      if (!key) return;
      setBusy("load");
      setError("");
      setNotice("");
      setReport(null);
      setIsNew(false);
      setRenaming(false);
      try {
        const data = await api<SourceDetail>(
          `/api/strategies/${encodeURIComponent(key)}/source`,
        );
        setDetail(data);
        setCode(data.code);
        setOriginal(data.code);
        setNewKey(key === "my_strategy" ? "my_strategy_v2" : `${key}_v2`);
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : String(caught));
      } finally {
        setBusy("");
      }
    },
    [],
  );

  useEffect(() => {
    setSelected(value);
    void load(value);
  }, [value, load]);

  const dirty = code !== original;
  const readOnly = Boolean(detail?.builtin) && !isNew;

  const startNew = () => {
    setIsNew(true);
    setDetail(null);
    setCode(TEMPLATE);
    setOriginal("");
    setNewKey("my_strategy");
    setReport(null);
    setError("");
    setNotice("新策略：改好 key 后点「保存并重载」。");
  };

  const fork = () => {
    if (!detail) return;
    const key = `${detail.key}_copy`;
    const next = code.replace(
      /^(\s*)key\s*=\s*["'][^"']*["']/m,
      `$1key = "${key}"`,
    );
    setIsNew(true);
    setDetail({ ...detail, key, builtin: false, editable: true, filename: "" });
    setCode(next);
    setOriginal("");
    setNewKey(key);
    setReport(null);
    setError("");
    setNotice(`已复制为「${key}」，保存后会生成新文件，原策略不变。`);
  };

  /** Rename the plugin: new key (and file name) plus an optional display name.
   * The file moves, the class key is rewritten and the registry reloads, so the
   * backtest page and the auto trader see the new name immediately. */
  const startRename = () => {
    if (!detail) return;
    setRenaming(true);
    setRenameKey(detail.key.endsWith("_copy") ? detail.key : detail.key + "_v2");
    setRenameName(detail.name ?? "");
    setError("");
    setNotice("");
  };

  const submitRename = async () => {
    if (!detail) return;
    const nextKey = renameKey.trim();
    if (!nextKey) {
      setError("请填写新的策略 key");
      return;
    }
    setRenameBusy(true);
    setError("");
    setNotice("");
    try {
      const result = await api<{ key: string; filename: string }>(
        "/api/strategies/" + encodeURIComponent(detail.key) + "/rename",
        {
          method: "POST",
          body: JSON.stringify({ new_key: nextKey, new_name: renameName.trim() }),
        },
      );
      setRenaming(false);
      setSelected(result.key);
      setNotice("已重命名并热加载：" + result.filename);
      onSelect(result.key);
      onSaved?.(result.key);
      void load(result.key);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setRenameBusy(false);
    }
  };

  const validate = async () => {
    setBusy("validate");
    setError("");
    setNotice("");
    try {
      const result = await api<ValidationReport>("/api/strategies/validate", {
        method: "POST",
        body: JSON.stringify({ code }),
      });
      setReport(result);
      if (result.ok) {
        setNotice(`静态检查通过${result.key ? ` · key=${result.key}` : ""}`);
      } else {
        setError("静态检查未通过，见下方问题列表。");
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy("");
    }
  };

  const save = async () => {
    setBusy("save");
    setError("");
    setNotice("");
    try {
      const key = isNew ? newKey.trim() : selected;
      if (isNew) {
        await api("/api/strategies", {
          method: "POST",
          body: JSON.stringify({ key, code, create: true }),
        });
      } else {
        await api(`/api/strategies/${encodeURIComponent(selected)}/source`, {
          method: "PUT",
          body: JSON.stringify({ code }),
        });
      }
      setOriginal(code);
      setIsNew(false);
      setDetail((current) =>
        current
          ? { ...current, key, code, editable: true, builtin: false, filename: `${key}.py` }
          : current,
      );
      setNotice(`已保存并热加载：${key}.py`);
      onSelect(key);
      onSaved?.(key);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy("");
    }
  };

  return (
    <div className="strategy-editor">
      <div className="strategy-editor-bar">
        <div className="strategy-editor-pick">
          <label className="field-label" htmlFor="strategy-editor-select">
            <span>策略文件</span>
            <em>{detail?.builtin ? "内置 · 只读" : detail?.filename || "未保存"}</em>
          </label>
          <select
            id="strategy-editor-select"
            className="select"
            value={isNew ? "" : selected}
            onChange={(event) => {
              if (event.target.value) {
                setIsNew(false);
                setSelected(event.target.value);
                onSelect(event.target.value);
              }
            }}
          >
            {isNew && <option value="">新策略（未保存）</option>}
            {strategies.map((item) => (
              <option key={item.key} value={item.key}>
                {item.name} · {item.key}
              </option>
            ))}
          </select>
        </div>
        <div className="strategy-editor-actions">
          <button type="button" className="btn-sm" onClick={startNew} disabled={busy !== ""}>
            新建
          </button>
          {readOnly && (
            <button type="button" className="btn-sm" onClick={fork} disabled={busy !== ""}>
              复制为新策略
            </button>
          )}
          <button
            type="button"
            className="btn-sm"
            onClick={startRename}
            disabled={busy !== "" || isNew || !detail?.editable || detail?.builtin}
            title={
              detail?.builtin
                ? "内置策略只读。先「复制为新策略」再改名"
                : "改文件名和 key，引用会自动跟上"
            }
          >
            重命名
          </button>
          <button
            type="button"
            className="btn-sm"
            onClick={() => void validate()}
            disabled={busy !== "" || !code.trim()}
          >
            {busy === "validate" ? "校验中…" : "校验"}
          </button>
          <button
            type="button"
            className="btn-sm primary"
            onClick={() => void save()}
            disabled={busy !== "" || readOnly || !code.trim() || (!dirty && !isNew)}
          >
            {busy === "save" ? "保存中…" : "保存并重载"}
          </button>
          {onClose && (
            <button type="button" className="btn-sm" onClick={onClose}>
              关闭
            </button>
          )}
        </div>
      </div>

      {isNew && (
        <div className="strategy-editor-newkey">
          <label className="field-label" htmlFor="strategy-new-key">
            <span>新策略 key</span>
            <em>小写字母开头，只能包含字母、数字和下划线</em>
          </label>
          <input
            id="strategy-new-key"
            className="input"
            value={newKey}
            onChange={(event) => setNewKey(event.target.value)}
            spellCheck={false}
          />
        </div>
      )}

      {renaming && detail && (
        <div className="strategy-editor-newkey">
          <label className="field-label" htmlFor="strategy-rename-key">
            <span>新 key</span>
            <em>小写字母开头。文件名会一起改，引用自动迁移</em>
          </label>
          <input
            id="strategy-rename-key"
            className="input"
            value={renameKey}
            spellCheck={false}
            onChange={(event) => setRenameKey(event.target.value)}
          />
          <label className="field-label" htmlFor="strategy-rename-name">
            <span>显示名</span>
            <em>可选</em>
          </label>
          <input
            id="strategy-rename-name"
            className="input"
            value={renameName}
            onChange={(event) => setRenameName(event.target.value)}
          />
          <div className="row" style={{ gap: 8 }}>
            <button
              type="button"
              className="btn-sm primary"
              disabled={renameBusy || !renameKey.trim()}
              onClick={() => void submitRename()}
            >
              {renameBusy ? "重命名中…" : "确认重命名"}
            </button>
            <button
              type="button"
              className="btn-sm"
              disabled={renameBusy}
              onClick={() => setRenaming(false)}
            >
              取消
            </button>
            <span className="faint" style={{ fontSize: "var(--fs-12)"}}>
              自动交易或 AI 交易正在使用这个策略时会被拒绝。
            </span>
          </div>
        </div>
      )}

      {error && <div className="banner error">{error}</div>}
      {notice && <div className="banner ok">{notice}</div>}

      <div className="strategy-editor-code">
        {busy === "load" ? (
          <div className="faint" style={{ padding: 16 }}>
            读取源码…
          </div>
        ) : (
          <CodeMirror
            value={code}
            height="440px"
            theme="light"
            readOnly={readOnly}
            extensions={[python()]}
            onChange={(next) => {
              setCode(next);
              setReport(null);
            }}
            basicSetup={{
              lineNumbers: true,
              foldGutter: true,
              highlightActiveLine: true,
              highlightActiveLineGutter: true,
              bracketMatching: true,
              closeBrackets: true,
              autocompletion: true,
              indentOnInput: true,
            }}
          />
        )}
      </div>

      {report && (
        <div className={`strategy-editor-report ${report.ok ? "ok" : "error"}`}>
          <div className="strategy-editor-report-head">
            <b>{report.ok ? "静态检查通过" : "静态检查未通过"}</b>
            {report.key && <span className="faint">key = {report.key}</span>}
          </div>
          {(report.issues ?? []).length > 0 && (
            <ul>
              {(report.issues ?? []).map((issue, index) => (
                <li key={`${issue.code ?? "issue"}-${index}`}>
                  {issue.line ? `第 ${issue.line} 行：` : ""}
                  {issue.message ?? issue.code ?? "未知问题"}
                </li>
              ))}
            </ul>
          )}
          {(report.imports ?? []).length > 0 && (
            <div className="faint">导入：{(report.imports ?? []).join(", ")}</div>
          )}
        </div>
      )}

      <div className="strategy-editor-foot faint">
        {selectedMeta?.description || "保存后策略会自动重新加载，回测页即可直接选用。"}
        {dirty && !isNew && " · 有未保存修改"}
      </div>
    </div>
  );
}
