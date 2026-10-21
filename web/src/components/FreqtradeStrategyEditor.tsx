import { useEffect, useState } from "react";
import CodeMirror from "@uiw/react-codemirror";
import { python } from "@codemirror/lang-python";
import { api } from "../api";

interface Props {
  name: string;
  onClose: () => void;
  onSaved?: () => void;
}

/** Editor for sidecar strategies under user_data/strategies. Freqtrade reloads
 * strategy code on restart; the panel tells the user to restart the bot. */
export default function FreqtradeStrategyEditor({ name, onClose, onSaved }: Props) {
  const [code, setCode] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api<{ name: string; code: string }>(
      `/api/ft/strategies/${encodeURIComponent(name)}/source`,
    )
      .then((data) => setCode(data.code ?? ""))
      .catch((caught) =>
        setError(caught instanceof Error ? caught.message : String(caught)),
      );
  }, [name]);

  const save = async () => {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await api(`/api/ft/strategies/${encodeURIComponent(name)}`, {
        method: "PUT",
        body: JSON.stringify({ code }),
      });
      setNotice("已保存。重启 Freqtrade 引擎后生效。");
      onSaved?.();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal-mask" onClick={onClose}>
      <div
        className="card strategy-editor-modal"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="card-head">
          <span className="card-title">Freqtrade 策略 · {name}.py</span>
          <span className="spacer" />
          <button type="button" className="btn-sm" onClick={onClose}>
            关闭
          </button>
        </div>
        <div className="card-body stack">
          {error && <div className="banner error">{error}</div>}
          {notice && <div className="banner ok">{notice}</div>}
          <CodeMirror
            value={code}
            height="520px"
            theme="light"
            extensions={[python()]}
            onChange={setCode}
          />
          <div className="strategy-editor-actions">
            <button
              type="button"
              className="btn primary"
              disabled={busy || !code.trim()}
              onClick={() => void save()}
            >
              {busy ? "保存中…" : "保存"}
            </button>
            <span className="faint" style={{ fontSize: 11 }}>
              保存前会做 Python 语法和 IStrategy 继承检查。
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}
