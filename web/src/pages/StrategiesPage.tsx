import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import type { Strategy } from "../types";
import StrategyEditor, { type StrategyEditorHandle } from "../components/StrategyEditor";

/** 策略页：左边一份策略清单，右边一个编辑器。
 *
 * 走的是「方案 A」：源码编辑从回测页的弹窗搬到这里，内置策略也能直接改，
 * 保存后落成用户目录里的同名覆盖文件，改坏了有「还原内置」退回原版。
 */

interface Props {
  /** 从回测页或实验室跳过来时预选的那条策略。 */
  focus?: string | null;
}

export default function StrategiesPage({ focus }: Props) {
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [selected, setSelected] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const editor = useRef<StrategyEditorHandle>(null);

  const refresh = useCallback(async (selectKey?: string) => {
    setLoading(true);
    setError("");
    try {
      const data = await api<{ items: Strategy[] }>("/api/strategies");
      const items = data.items ?? [];
      setStrategies(items);
      setSelected((current) => {
        const want = selectKey || current;
        if (want && items.some((item) => item.key === want)) return want;
        return items[0]?.key ?? "";
      });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh(focus ?? undefined);
  }, [refresh, focus]);

  /** 内置被覆盖的仍算内置：它显示在「内置」组里，只是挂一个「已覆盖」。 */
  const customItems = useMemo(
    () => strategies.filter((item) => !item.builtin),
    [strategies],
  );
  const builtinItems = useMemo(
    () => strategies.filter((item) => item.builtin),
    [strategies],
  );

  const row = (item: Strategy) => (
    <li key={item.key}>
      <button
        type="button"
        className={`cand-item${item.key === selected ? " active" : ""}`}
        onClick={() => setSelected(item.key)}
      >
        <span className="cand-top">
          <b>{item.name}</b>
          {item.overridden && <span className="cand-state">已覆盖</span>}
        </span>
        <span className="cand-meta faint">{item.key}</span>
        <span className="cand-metrics">
          <span>参数 {(item.params ?? []).length} 个</span>
        </span>
      </button>
    </li>
  );

  return (
    <>
      <div className="page-head">
        <h1 className="page-title">策略</h1>
        <span className="page-sub">内置策略可以直接改，改坏了一键还原</span>
        <span className="spacer" />
        <span className="faint" style={{ fontSize: "var(--fs-12)"}}>
          {loading ? "读取中…" : `${strategies.length} 个策略`}
        </span>
      </div>

      <div className="page-toolbar">
        <button
          type="button"
          className="btn-sm"
          onClick={() => editor.current?.startNew()}
        >
          新建
        </button>
        <button
          type="button"
          className="btn-sm"
          disabled={!selected}
          onClick={() => editor.current?.fork()}
        >
          复制为新策略
        </button>
        <button type="button" className="btn-sm" onClick={() => void refresh()}>
          刷新
        </button>
        <span className="spacer" />
        <span className="faint" style={{ fontSize: "var(--fs-12)"}}>
          保存后自动热加载，回测页直接可选
        </span>
      </div>

      {error && <div className="banner error">{error}</div>}

      <div className="strategy-page">
        <section className="card">
          <div className="card-head">
            <span className="card-title">策略清单</span>
            <span className="spacer" />
            <span className="faint" style={{ fontSize: "var(--fs-12)"}}>
              自定义 {customItems.length} · 内置 {builtinItems.length}
            </span>
          </div>
          <div className="card-body">
            {strategies.length === 0 ? (
              <div className="empty-note">
                {loading ? "读取中…" : "还没有策略。点「新建」写一个。"}
              </div>
            ) : (
              <ul className="cand-list">
                {customItems.length > 0 && (
                  <Fragment key="custom">
                    <li className="strategy-group">自定义 · {customItems.length}</li>
                    {customItems.map(row)}
                  </Fragment>
                )}
                {builtinItems.length > 0 && (
                  <Fragment key="builtin">
                    <li className="strategy-group">内置 · {builtinItems.length}</li>
                    {builtinItems.map(row)}
                  </Fragment>
                )}
              </ul>
            )}
          </div>
        </section>

        <section className="card">
          <div className="card-head">
            <span className="card-title">策略源码</span>
            <span className="spacer" />
            <span className="faint" style={{ fontSize: "var(--fs-12)"}}>
              左侧选策略
            </span>
          </div>
          <div className="card-body">
            {/* 一条策略都没有时也挂着编辑器：「新建」要靠它的 ref 触发。 */}
            <StrategyEditor
              ref={editor}
              layout="page"
              strategies={strategies}
              value={selected}
              onSelect={(key) => setSelected(key)}
              onSaved={(key) => void refresh(key)}
            />
          </div>
        </section>
      </div>
    </>
  );
}
