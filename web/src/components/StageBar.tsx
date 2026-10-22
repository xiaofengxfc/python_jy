/** 阶段条：一条流程走到哪一步。AI 量化页和策略实验室共用。 */

export interface StageSpec {
  key: string;
  label: string;
}

interface Props {
  stages: StageSpec[];
  /** 当前阶段的 key。找不到就停在第一步。 */
  current: string;
}

export default function StageBar({ stages, current }: Props) {
  const index = Math.max(
    0,
    stages.findIndex((stage) => stage.key === current),
  );

  return (
    <ol className="stage-bar">
      {stages.map((stage, position) => (
        <li
          key={stage.key}
          data-state={position < index ? "done" : position === index ? "active" : "todo"}
        >
          <span className="stage-index">{position + 1}</span>
          <span className="stage-label">{stage.label}</span>
        </li>
      ))}
    </ol>
  );
}
