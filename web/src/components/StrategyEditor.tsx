import { useCallback, useEffect, useImperativeHandle, useMemo, useState } from "react";
import type { Ref } from "react";
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
  /** 当前生效的是用户目录里的覆盖文件（存在同名内置原版）。 */
  overridden: boolean;
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

/** 「新建」「复制为新策略」两个动作的状态在编辑器里，策略页的工具条要能远程触发。 */
export interface StrategyEditorHandle {
  startNew: () => void;
  fork: () => void;
}

interface Props {
  strategies: Strategy[];
  /** Currently selected strategy key in the page. */
  value: string;
  onSelect: (key: string) => void;
  onSaved?: (key: string) => void;
  onClose?: () => void;
  /** page：独立策略页里用，代码区撑满卡片、内部不再渲染策略下拉。 */
  layout?: "modal" | "page";
  ref?: Ref<StrategyEditorHandle>;
}

/** Inline strategy source editor: read, validate, save and hot-reload a user
 * plugin. Built-ins open editable; saving one writes a same-key override into
 * the user directory, and 「还原内置」 deletes that file again. */
export default function StrategyEditor({
  strategies,
  value,
  onSelect,
  onSaved,
  onClose,
  layout = "modal",
  ref,
}: Props) {
  const page = layout === "page";
  const [selected, setSelected] = useState(value);
  const [detail, setDetail] = useState<SourceDetail | null>(null);
  const [code, setCode] = useState("");
  const [original, setOriginal] = useState("");
  const [isNew, setIsNew] = useState(false);
  const [newKey, setNewKey] = useState("my_strategy");
  const [busy, setBusy] = useState<"" | "load" | "save" | "validate" | "restore">("");
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
  /** 内置也带 editable:true（保存会写成覆盖文件），所以只看后端给的这一位。 */
  const readOnly = !isNew && detail !== null && !detail.editable;
  /** 状态行：内置看有没有被覆盖，自定义看文件名。 */
  const sourceLabel = detail?.builtin
    ? detail.overridden
      ? `内置 · 已覆盖 · ${detail.filename}`
      : "内置 · 未覆盖"
    : detail?.filename || "未保存";

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
    setDetail({
      ...detail,
      key,
      builtin: false,
      overridden: false,
      editable: true,
      filename: "",
    });
    setCode(next);
    setOriginal("");
    setNewKey(key);
    setReport(null);
    setError("");
    setNotice(`已复制为「${key}」，保存后会生成新文件，原策略不变。`);
  };

  useImperativeHandle(ref, () => ({ startNew, fork }));

  /** Drop the override file and go back to the package copy. Only offered when
   * the current source is an override, which the detail payload tells us. */
  const restore = async () => {
    if (!detail) return;
    const key = detail.key;
    setBusy("restore");
    setError("");
    setNotice("");
    try {
      await api(`/api/strategies/${encodeURIComponent(key)}/restore`, { method: "POST" });
      await load(key);
      setNotice("已还原内置原版，覆盖文件已删除。");
      onSaved?.(key);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy("");
    }
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
      let result: { filename?: string; builtin?: boolean; overridden?: boolean } = {};
      if (isNew) {
        await api("/api/strategies", {
          method: "POST",
          body: JSON.stringify({ key, code, create: true }),
        });
      } else {
        result =
          (await api<{ filename?: string; builtin?: boolean; overridden?: boolean }>(
            `/api/strategies/${encodeURIComponent(selected)}/source`,
            {
              method: "PUT",
              body: JSON.stringify({ code }),
            },
          )) ?? {};
      }
      const filename = result.filename || `${key}.py`;
      const overridesBuiltin = Boolean(result.builtin);
      setOriginal(code);
      setIsNew(false);
      setDetail((current) =>
        current
          ? {
              ...current,
              key,
              code,
              editable: true,
              filename,
              builtin: overridesBuiltin,
              overridden: Boolean(result.overridden) || overridesBuiltin,
            }
          : current,
      );
      setNotice(
        overridesBuiltin
          ? `已保存并热加载：${filename} · 覆盖内置原版，可点「还原内置」退回`
          : `已保存并热加载：${filename}`,
      );
      onSelect(key);
      onSaved?.(key);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy("");
    }
  };

  return (
    <div className={page ? "strategy-editor page" : "strategy-editor"}>
      <div className="strategy-editor-bar">
        <div className="strategy-editor-pick">
          {page ? (
            /* 页面模式由左边的列表负责挑策略，这里只报一下当前是哪个文件。 */
            <div className="field-label">
              <span>{detail?.name || (isNew ? "新策略（未保存）" : selected)}</span>
              <em>{isNew ? "未保存" : sourceLabel}</em>
            </div>
          ) : (
            <>
              <label className="field-label" htmlFor="strategy-editor-select">
                <span>策略文件</span>
                <em>{sourceLabel}</em>
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
            </>
          )}
        </div>
        <div className="strategy-editor-actions">
          {!page && (
            <>
              <button type="button" className="btn-sm" onClick={startNew} disabled={busy !== ""}>
                新建
              </button>
              <button type="button" className="btn-sm" onClick={fork} disabled={busy !== "" || !detail}>
                复制为新策略
              </button>
            </>
          )}
          <button
            type="button"
            className="btn-sm"
            onClick={startRename}
            disabled={busy !== "" || isNew || !detail?.editable || detail?.builtin}
            title={
              detail?.builtin
                ? "内置策略不能改名，先还原或另存为新策略"
                : "改文件名和 key，引用会自动跟上"
            }
          >
            重命名
          </button>
          {detail?.overridden && !isNew && (
            <button
              type="button"
              className="btn-sm"
              onClick={() => void restore()}
              disabled={busy !== ""}
              title="删掉你的覆盖文件，回到内置原版"
            >
              {busy === "restore" ? "还原中…" : "还原内置"}
            </button>
          )}
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
            height={page ? "100%" : "440px"}
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
