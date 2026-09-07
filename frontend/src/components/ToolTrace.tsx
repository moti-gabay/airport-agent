import { useState } from "react";
import type { ToolCall } from "../api";

/** The tool calls behind one answer. Collapsed by default: it is evidence, not the answer. */
export function ToolTrace({ trace }: { trace: ToolCall[] }) {
  const [open, setOpen] = useState(false);
  if (trace.length === 0) return null;

  return (
    <div className="trace">
      <button className="trace-toggle" onClick={() => setOpen(!open)}>
        {open ? "▾" : "▸"} {trace.length} tool call{trace.length > 1 ? "s" : ""}
        <span className="trace-names">
          {trace.map((t) => t.name).join(", ")}
        </span>
      </button>
      {open && (
        <ol className="trace-list">
          {trace.map((t, i) => (
            <li key={i} className={t.ok ? "" : "trace-error"}>
              <code>
                {t.name}({JSON.stringify(t.input)})
              </code>
              <span className="trace-summary">{t.summary}</span>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
