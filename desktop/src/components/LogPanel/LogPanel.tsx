import { useEffect, useMemo, useRef, useState } from "react";

import type { LogsSnapshot } from "../../api/types";
import { parseLogStatusRows } from "../../state/logStatus";

type Props = {
  logs: LogsSnapshot | null;
  error: string | null;
  notice: string | null;
};

export function LogPanel({ logs, error, notice }: Props): JSX.Element {
  const [activeTab, setActiveTab] = useState<"raw" | "status">("raw");
  const [search, setSearch] = useState("");
  const [limit, setLimit] = useState(500);
  const [autoScroll, setAutoScroll] = useState(true);
  const [showMeasurement, setShowMeasurement] = useState(false);
  const terminalRef = useRef<HTMLPreElement | null>(null);
  const rows = logs?.rows ?? [];
  const visibleRows = useMemo(() => {
    const categoryFiltered = showMeasurement ? rows : rows.filter((row) => row.category !== "MEASUREMENT");
    const filtered = search
      ? categoryFiltered.filter((row) => `${row.severity} ${row.source} ${row.channel} ${row.tag} ${row.rawText}`.toLowerCase().includes(search.toLowerCase()))
      : categoryFiltered;
    return filtered.slice(-limit);
  }, [limit, rows, search, showMeasurement]);
  const statusItems = useMemo(() => parseLogStatusRows(rows), [rows]);

  useEffect(() => {
    if (autoScroll && terminalRef.current) {
      terminalRef.current.scrollTop = terminalRef.current.scrollHeight;
    }
  }, [autoScroll, visibleRows]);

  async function copyVisible(): Promise<void> {
    await navigator.clipboard?.writeText(formatRows(visibleRows));
  }

  return (
    <section className="logPanel">
      <div className="panelHeader panelHeaderWithActions">
        <div className="tabs">
          <button className={activeTab === "raw" ? "active" : ""} onClick={() => setActiveTab("raw")}>
            Raw Log
          </button>
          <button className={activeTab === "status" ? "active" : ""} onClick={() => setActiveTab("status")}>
            Status
          </button>
        </div>
        {activeTab === "raw" ? (
          <div className="logTools">
            <input value={search} placeholder="Filter logs" onChange={(event) => setSearch(event.target.value)} />
            <select value={limit} onChange={(event) => setLimit(Number(event.target.value))}>
              <option value={500}>Latest 500</option>
              <option value={1000}>Latest 1000</option>
            </select>
            <label className="checkLine compact">
              <input type="checkbox" checked={autoScroll} onChange={(event) => setAutoScroll(event.target.checked)} />
              Auto-scroll
            </label>
            <label className="checkLine compact">
              <input
                type="checkbox"
                checked={showMeasurement}
                onChange={(event) => setShowMeasurement(event.target.checked)}
              />
              Show Measurement Data
            </label>
            <button onClick={() => void copyVisible()}>Copy visible</button>
          </div>
        ) : null}
      </div>
      {error ? <div className="inlineError">{error}</div> : null}
      {notice ? <div className="inlineNotice">{notice}</div> : null}
      {activeTab === "raw" ? (
        <pre ref={terminalRef} className="logTerminal">
          {formatRows(visibleRows)}
        </pre>
      ) : (
        <div className="statusList">
          {statusItems.map((item) => (
            <article key={`${item.category}-${item.title}`} className="statusCard">
              <div className="statusCardHeader">
                <span className={`severityBadge ${item.severity}`}>{item.severity}</span>
                <strong>{item.category}</strong>
                <span>{item.title}</span>
                {item.lastSeen ? <time>{item.lastSeen}</time> : null}
              </div>
              <p>{item.explanation}</p>
              <dl>
                {Object.entries(item.details).map(([key, value]) => (
                  <div key={key}>
                    <dt>{key}</dt>
                    <dd>{String(value ?? "NA")}</dd>
                  </div>
                ))}
              </dl>
            </article>
          ))}
          {statusItems.length ? null : <div className="trendEmpty">No status yet</div>}
        </div>
      )}
    </section>
  );
}

function formatRows(rows: NonNullable<LogsSnapshot["rows"]>): string {
  return rows.map((row) => `[${row.severity}] ${row.source}/${row.channel} ${row.tag}: ${truncate(row.rawText, 240)}`).join("\n");
}

function truncate(value: string, limit: number): string {
  return value.length <= limit ? value : `${value.slice(0, limit)}...`;
}
