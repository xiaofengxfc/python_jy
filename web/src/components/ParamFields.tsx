import type { ParamSpec } from "../types";

interface Props {
  specs: ParamSpec[];
  values: Record<string, unknown>;
  onChange: (name: string, value: unknown) => void;
}

function kindOf(spec: ParamSpec): "choice" | "bool" | "number" | "text" {
  if (spec.choices.length > 0) return "choice";
  if (typeof spec.default === "boolean") return "bool";
  if (typeof spec.default === "number") return "number";
  return "text";
}

/** Renders a strategy's declared parameters; every strategy uses the same
 * layout so switching strategies never reshuffles the panel. */
export function ParamFields({ specs, values, onChange }: Props) {
  if (!specs.length) {
    return <div className="faint" style={{ fontSize: "var(--fs-13)"}}>该策略没有可调参数。</div>;
  }

  return (
    <div className="form-grid">
      {specs.map((spec) => {
        const kind = kindOf(spec);
        const value = values[spec.name] ?? spec.default;
        if (kind === "bool") {
          return (
            <label key={spec.name} className="check">
              <input
                type="checkbox"
                checked={Boolean(value)}
                onChange={(event) => onChange(spec.name, event.target.checked)}
              />
              <span>{spec.label}</span>
            </label>
          );
        }
        return (
          <div key={spec.name} className="field">
            <label className="field-label" htmlFor={`param-${spec.name}`}>
              <span>{spec.label}</span>
              {spec.help && <em>{spec.help}</em>}
            </label>
            {kind === "choice" ? (
              <select
                id={`param-${spec.name}`}
                className="select"
                value={String(value)}
                onChange={(event) => onChange(spec.name, event.target.value)}
              >
                {spec.choices.map((choice) => (
                  <option key={String(choice)} value={String(choice)}>
                    {String(choice)}
                  </option>
                ))}
              </select>
            ) : (
              <input
                id={`param-${spec.name}`}
                className="input"
                type="number"
                inputMode="decimal"
                min={spec.minimum ?? undefined}
                max={spec.maximum ?? undefined}
                step={spec.step ?? "any"}
                value={String(value)}
                onChange={(event) => onChange(spec.name, Number(event.target.value))}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}

export type { ParamSpec };
