import type { LogsSnapshot } from "../../api/types";

type Props = {
  logs: LogsSnapshot | null;
  error: string | null;
};

export function LogPanel({ logs, error }: Props): JSX.Element {
  const rows = logs?.rows ?? [];
  return (
    <section className="logPanel">
      <div className="panelHeader">Raw Log / Event Log</div>
      {error ? <div className="inlineError">{error}</div> : null}
      <pre className="logTerminal">
        {rows
          .slice(-220)
          .map((row) => `[${row.severity}] ${row.source}/${row.channel} ${row.tag}: ${truncate(row.rawText, 240)}`)
          .join("\n")}
      </pre>
    </section>
  );
}

function truncate(value: string, limit: number): string {
  return value.length <= limit ? value : `${value.slice(0, limit)}...`;
}
