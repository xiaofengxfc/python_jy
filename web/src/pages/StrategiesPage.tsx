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
  /** 删除成功那一句：refresh() 会把编辑器里的通知清掉，所以放在页面这一层。 */
  const [notice, setNotice] = useState("");
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

  /** 编辑器里有没保存的改动时先问一句：换策略等于把它丢掉。
   *  这里用的是 app 里其他破坏性操作同一个办法（window.confirm），不另做弹窗。 */
  const confirmDiscard = (what: string) =>
    !editor.current?.hasUnsaved() ||
    window.confirm(`当前策略还有没保存的修改，${what}就丢了。继续？`);

  const pick = (key: string) => {
    if (key === selected) return;
    setNotice("");
    if (!confirmDiscard(`切到「${key}」`)) return;
    setSelected(key);
  };

  const startNew = () => {
    if (!confirmDiscard("新建")) return;
    editor.current?.startNew();
  };

  const fork = () => {
    if (!confirmDiscard("复制为新策略")) return;
    editor.current?.fork();
  };

  const row = (item: Strategy) => (
    <li key={item.key}>
      <button
        type="button"
        className={`cand-item${item.key === selected ? " active" : ""}`}
        onClick={() => pick(item.key)}
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
          onClick={startNew}
        >
          新建
        </button>
        <button
          type="button"
          className="btn-sm"
          disabled={!selected}
          onClick={fork}
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
      {notice && <div className="banner ok">{notice}</div>}

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
              onDeleted={(key) => setNotice(`已删除「${key}」，文件已从数据目录移除。`)}
            />
          </div>
        </section>
      </div>
    </>
  );
}
