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
          .map((row) => `[${row.severity}] ${row.source}/${row.channel} ${row.tag}: ${row.rawText}`)
          .join("\n")}
      </pre>
    </section>
  );
}

