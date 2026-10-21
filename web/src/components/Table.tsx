import type { ReactNode } from "react";

interface Props {
  headers: string[];
  rows: ReactNode[][];
  empty: string;
  rightFrom?: number;
  /** Optional row activation, e.g. opening a detail dialog for that row. */
  onRowClick?: (index: number) => void;
}

export default function Table({
  headers,
  rows,
  empty,
  rightFrom = 3,
  onRowClick,
}: Props) {
  if (!rows.length) return <div className="empty">{empty}</div>;
  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            {headers.map((header, index) => (
              <th key={header} className={index >= rightFrom ? "right" : undefined}>
                {header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, rowIndex) => (
            <tr
              key={rowIndex}
              className={onRowClick ? "clickable" : undefined}
              onClick={onRowClick ? () => onRowClick(rowIndex) : undefined}
            >
              {row.map((cell, cellIndex) => (
                <td key={cellIndex} className={cellIndex >= rightFrom ? "right" : undefined}>
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
