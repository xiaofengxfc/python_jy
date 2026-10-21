import { useMemo } from "react";
import type { Strategy } from "../types";
import { ParamFields } from "./ParamFields";

interface Props {
  /** Local strategies from `/api/strategies` (built-ins + user plugins). */
  strategies: Strategy[];
  value: string;
  params: Record<string, unknown>;
  disabled?: boolean;
  onChange: (key: string) => void;
  onParams: (params: Record<string, unknown>) => void;
}

/** Pick a local biTrader strategy to run on the Freqtrade engine.
 *
 * The backend renders an `IStrategy` adapter for whatever is chosen here, so
 * the panel does not need to know anything about the two strategy formats —
 * but the parameters do have to be sent, because they are baked into the
 * generated adapter. */
export default function FreqtradeLocalPicker({
  strategies,
  value,
  params,
  disabled,
  onChange,
  onParams,
}: Props) {
  const selected = useMemo(
    () => strategies.find((item) => item.key === value) ?? null,
    [strategies, value],
  );

  return (
    <>
      <div className="field">
        <label className="field-label" htmlFor="ft-local-strategy">
          <span>本地策略</span>
          <em>自动转成 IStrategy 适配器</em>
        </label>
        <select
          id="ft-local-strategy"
          className="select"
          value={value}
          disabled={disabled || strategies.length === 0}
          onChange={(event) => {
            const key = event.target.value;
            const found = strategies.find((item) => item.key === key);
            onChange(key);
            onParams(found ? { ...found.defaults } : {});
          }}
        >
          {strategies.length === 0 && <option value="">（没有本地策略）</option>}
          {strategies.map((item) => (
            <option key={item.key} value={item.key}>
              {item.name} · {item.key}
            </option>
          ))}
        </select>
        <div className="hint">
          {selected
            ? selected.editable
              ? "用户策略 " + (selected.source_name || selected.key + ".py")
              : "内置策略 · 只读"
            : "先选一个策略"}
        </div>
      </div>
      {(selected?.params ?? []).length > 0 && (
        <div className="field">
          <label className="field-label">
            <span>策略参数</span>
            <em>写进生成的适配器</em>
          </label>
          <ParamFields
            specs={selected?.params ?? []}
            values={params}
            onChange={(name, next) => onParams({ ...params, [name]: next })}
          />
        </div>
      )}
    </>
  );
}
