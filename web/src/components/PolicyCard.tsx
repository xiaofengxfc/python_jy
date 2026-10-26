import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { AIAutoSettings, AIPolicy, AIPolicyReview } from "../types";

/**
 * 决策准则：一段人确认过的文本 + 参数补丁 + 版本历史。
 *
 * 模型只负责提建议（「AI 复盘」），落盘全靠人点两下：保存文本、应用选中参数。
 * 撤销也是一等操作——每次应用都留了应用之前的快照，回滚只是把那份快照再应用一次。
 */

interface Props {
  /** 运行中也能改，但只对下一次启动生效；界面用一句话说清，不禁用。 */
  running: boolean;
  /** 当前 ai_auto，用来显示「现在是几」。 */
  auto: AIAutoSettings | null;
  /** 参数被补丁改过之后交回页面，表单要跟着变。 */
  onApplied?: (next: Partial<AIAutoSettings>) => void;
}

const TEXT_LIMIT = 4000;
const HISTORY_SHOWN = 6;
const SEVERITY_LABEL: Record<string, string> = { high: "高", medium: "中", low: "低" };

function clockText(seconds?: number): string {
  if (!seconds) return "还没改过";
  const date = new Date(seconds * 1000);
  const pad = (value: number) => String(value).padStart(2, "0");
  return date.getMonth() + 1 + "-" + pad(date.getDate()) + " " + pad(date.getHours()) + ":" + pad(date.getMinutes());
}

function fmtValue(value: unknown): string {
  if (typeof value === "boolean") return value ? "是" : "否";
  if (Array.isArray(value)) return value.join(" / ") || "（空）";
  if (value === null || value === undefined) return "—";
  return String(value);
}

export default function PolicyCard({ running, auto, onApplied }: Props) {
  const [policy, setPolicy] = useState<AIPolicy | null>(null);
  const [text, setText] = useState("");
  const [review, setReview] = useState<AIPolicyReview | null>(null);
  const [picked, setPicked] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const load = useCallback(async () => {
    try {
      const next = await api<AIPolicy>("/api/ai/policy");
      setPolicy(next);
      setText(next.text);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const labelOf = (key: string) => policy?.fields.find((item) => item.key === key)?.label ?? key;
  const dirty = policy !== null && text.trim() !== policy.text;

  const runReview = async () => {
    setBusy("review");
    setError("");
    setNotice("");
    try {
      const result = await api<AIPolicyReview>("/api/ai/policy/review", {
        method: "POST",
        body: JSON.stringify({ limit: 30 }),
      });
      setReview(result);
      const next: Record<string, boolean> = {};
      for (const key of Object.keys(result.patch.overrides ?? {})) next[key] = true;
      setPicked(next);
      setNotice(
        result.findings.length
          ? "复盘完成：" + result.findings.length + " 条不足，建议在下面。"
          : "复盘完成，没找到具体不足。",
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy("");
    }
  };

  const apply = async (overrides: Record<string, unknown>, note: string, kind: string) => {
    setBusy(kind);
    setError("");
    setNotice("");
    try {
      const result = await api<{
        policy: AIPolicy;
        ai_auto: Partial<AIAutoSettings>;
        changed: Record<string, unknown>;
        warnings: string[];
      }>("/api/ai/policy/apply", {
        method: "POST",
        body: JSON.stringify({ text, overrides, note, author: "人工" }),
      });
      setPolicy(result.policy);
      setText(result.policy.text);
      const keys = Object.keys(result.changed ?? {});
      const tail = result.warnings?.length ? "（" + result.warnings.join("；") + "）" : "";
      setNotice(keys.length ? "已生效：" + keys.join("、") + tail : "准则已保存。" + tail);
      onApplied?.(result.ai_auto ?? {});
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy("");
    }
  };

  const applyText = () => void apply({}, "人工修改准则文本", "text");

  const applyPatch = () => {
    if (!review) return;
    const overrides: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(review.patch.overrides ?? {})) {
      if (picked[key]) overrides[key] = value;
    }
    void apply(overrides, review.patch.rationale || "复盘建议", "apply");
  };

  const rollback = async (version: number) => {
    const ok = window.confirm(
      "撤销 v" + version + "？参数会恢复成那一版应用之前的样子，准则文本用那一版的。",
    );
    if (!ok) return;
    setBusy("rollback");
    setError("");
    setNotice("");
    try {
      const result = await api<{
        policy: AIPolicy;
        ai_auto: Partial<AIAutoSettings>;
        warnings: string[];
      }>("/api/ai/policy/rollback", {
        method: "POST",
        body: JSON.stringify({ version }),
      });
      setPolicy(result.policy);
      setText(result.policy.text);
      setNotice("已撤销 v" + version + "，记成 v" + result.policy.version + "。");
      onApplied?.(result.ai_auto ?? {});
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy("");
    }
  };

  const suggestion = review?.patch;
  const suggestedKeys = suggestion ? Object.keys(suggestion.overrides ?? {}) : [];

  return (
    <section className="card">
      <div className="card-head">
        <span className="card-title">决策准则</span>
        <span className="hint">
          v{policy?.version ?? 0}
          {policy?.author ? " · " + policy.author : ""}
          {" · " + clockText(policy?.updated_at)}
        </span>
        <span className="spacer" />
        <button
          type="button"
          className="btn-sm"
          disabled={busy !== ""}
          onClick={() => void runReview()}
        >
          {busy === "review" ? "复盘中…" : "AI 复盘"}
        </button>
      </div>
      <div className="card-body">
        {error && <div className="banner error">{error}</div>}
        {notice && <div className="banner ok">{notice}</div>}
        <div className="field">
          <label className="field-label" htmlFor="ai-policy-text">
            <span>准则文本</span>
            <em>进每次决策的提示词。写纪律，不写策略代码；与上限冲突时以上限为准。</em>
          </label>
          <textarea
            id="ai-policy-text"
            className="input"
            rows={7}
            maxLength={TEXT_LIMIT}
            value={text}
            placeholder="例如：空仓时只在 4h 与主周期同向、且回踩不破前低时才开仓；连续两次被拒就等下一根。"
            onChange={(event) => setText(event.target.value)}
          />
          <div className="policy-foot">
            <span className="hint">
              {text.length} / {TEXT_LIMIT} 字
              {running ? " · 运行中改的只对下次启动生效" : ""}
            </span>
            <span className="spacer" />
            <button
              type="button"
              className="btn-sm"
              disabled={busy !== "" || !dirty}
              onClick={applyText}
            >
              {busy === "text" ? "保存中…" : "保存准则"}
            </button>
          </div>
        </div>

        {review && (
          <div className="policy-review">
            <div className="policy-row">
              <b>复盘（最近 {review.count} 条决策）</b>
              {review.expected_effect && <span className="hint">{review.expected_effect}</span>}
            </div>
            {review.findings.length === 0 && (
              <div className="empty-note">模型没找出具体不足。</div>
            )}
            {review.findings.map((item, index) => (
              <div className="policy-finding" key={index + "-" + item.issue}>
                <span className={"sev " + item.severity}>
                  {SEVERITY_LABEL[item.severity] ?? item.severity}
                </span>
                <span>{item.issue}</span>
                {item.evidence && <em>{item.evidence}</em>}
              </div>
            ))}

            {suggestion && suggestion.text && (
              <>
                <div className="policy-row">
                  <b>建议准则</b>
                  <span className="spacer" />
                  <button
                    type="button"
                    className="btn-sm"
                    onClick={() => setText(suggestion.text)}
                  >
                    填进编辑器
                  </button>
                </div>
                <div className="policy-preview">{suggestion.text}</div>
              </>
            )}

            {suggestion && suggestedKeys.length > 0 && (
              <table className="table">
                <thead>
                  <tr>
                    <th>应用</th>
                    <th>参数</th>
                    <th>现在</th>
                    <th>建议</th>
                  </tr>
                </thead>
                <tbody>
                  {suggestedKeys.map((key) => (
                    <tr key={key}>
                      <td>
                        <input
                          type="checkbox"
                          checked={picked[key] ?? false}
                          onChange={(event) =>
                            setPicked((prev) => ({ ...prev, [key]: event.target.checked }))
                          }
                        />
                      </td>
                      <td>{labelOf(key)}</td>
                      <td>{fmtValue((auto as Record<string, unknown> | null)?.[key])}</td>
                      <td>{fmtValue(suggestion.overrides[key])}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}

            {review.rejected.length > 0 && (
              <div className="hint">
                {"已丢弃：" + review.rejected.join("；")}
              </div>
            )}

            {suggestion && (suggestion.text || suggestedKeys.length > 0) && (
              <div className="policy-row">
                {suggestion.rationale && <em className="hint">{suggestion.rationale}</em>}
                <span className="spacer" />
                <button
                  type="button"
                  className="btn-sm primary"
                  disabled={busy !== ""}
                  onClick={applyPatch}
                >
                  {busy === "apply" ? "应用中…" : "应用选中参数 + 编辑器里的准则"}
                </button>
              </div>
            )}
          </div>
        )}

        {policy && policy.history.length > 0 && (
          <div className="policy-history">
            <div className="policy-row">
              <b>历史</b>
              <span className="hint">撤销会把那一版应用之前的参数恢复回来，并记成一版新的。</span>
            </div>
            {policy.history.slice(0, HISTORY_SHOWN).map((item) => (
              <div className="policy-row" key={item.version}>
                <span>v{item.version}</span>
                <span className="hint">{clockText(item.at)}</span>
                <span className="hint">{item.note || "（无说明）"}</span>
                <span className="spacer" />
                <button
                  type="button"
                  className="btn-sm"
                  disabled={busy !== ""}
                  onClick={() => void rollback(item.version)}
                >
                  撤销
                </button>
              </div>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}
