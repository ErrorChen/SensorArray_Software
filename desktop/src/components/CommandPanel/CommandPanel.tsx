import { Eraser, SendHorizontal } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import type { BackendHttpClient } from "../../api/httpClient";
import type { BackendSnapshotPayload, CommandLineEnding } from "../../api/types";
import { isCommandSendDisabled, updateCommandHistory } from "../../state/commandPanel";

type Props = {
  client: BackendHttpClient | null;
  snapshot: BackendSnapshotPayload | null;
  onError: (message: string) => void;
};

const historyStorageKey = "sensorarray.command.history";
const maxHistory = 20;

export function CommandPanel({ client, snapshot, onError }: Props): JSX.Element {
  const [commandText, setCommandText] = useState("");
  const [lineEnding, setLineEnding] = useState<CommandLineEnding>("lf");
  const [history, setHistory] = useState<string[]>(() => readHistory());
  const [records, setRecords] = useState<string[]>([]);
  const [pending, setPending] = useState(false);

  const connection = snapshot?.connection;
  const state = connection?.state ?? "disconnected";
  const activeTransport = connection?.mode ?? "serial";
  const isBusy = ["connecting", "disconnecting", "reconnecting"].includes(state);
  const isConnected = ["connected", "streaming"].includes(state);
  const sendDisabled = isCommandSendDisabled({
    hasClient: Boolean(client),
    pending,
    busy: isBusy,
    connected: isConnected,
    commandText
  });

  const transportLabel = useMemo(() => {
    if (!isConnected) {
      return "disconnected";
    }
    const device = connection?.deviceLabel ? ` ${connection.deviceLabel}` : "";
    return `${activeTransport}${device}`;
  }, [activeTransport, connection?.deviceLabel, isConnected]);

  useEffect(() => {
    window.localStorage.setItem(historyStorageKey, JSON.stringify(history.slice(0, maxHistory)));
  }, [history]);

  async function sendCommand(): Promise<void> {
    if (!client || sendDisabled) {
      return;
    }
    setPending(true);
    try {
      const response = await client.writeCommand({
        text: commandText,
        lineEnding,
        encoding: "utf-8",
        mode: "text"
      });
      if (!response.ok) {
        const message = response.error || "write failed";
        addRecord(`TX failed: ${message}`);
        onError(message);
        return;
      }
      addRecord(`TX(${response.transport ?? activeTransport}): ${truncate(commandText, 120)}`);
      rememberCommand(commandText);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      addRecord(`TX failed: ${message}`);
      onError(message);
    } finally {
      setPending(false);
    }
  }

  function addRecord(message: string): void {
    const time = new Date().toLocaleTimeString();
    setRecords((current) => [`[${time}] ${message}`, ...current].slice(0, 40));
  }

  function rememberCommand(value: string): void {
    const trimmed = value.trim();
    if (!trimmed) {
      return;
    }
    setHistory((current) => updateCommandHistory(current, trimmed, maxHistory));
  }

  return (
    <section className="commandPanel">
      <div className="panelHeader">Write / Command</div>
      <div className="commandMeta">Active transport: {transportLabel}</div>
      <textarea
        className="commandInput"
        value={commandText}
        placeholder="Enter command text"
        onChange={(event) => setCommandText(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && event.ctrlKey) {
            event.preventDefault();
            void sendCommand();
          }
        }}
      />
      <div className="commandControls">
        <select value={lineEnding} onChange={(event) => setLineEnding(event.target.value as CommandLineEnding)}>
          <option value="lf">Append LF</option>
          <option value="crlf">Append CRLF</option>
          <option value="none">No line ending</option>
        </select>
        <select
          value=""
          aria-label="Command history"
          onChange={(event) => {
            if (event.target.value) {
              setCommandText(event.target.value);
            }
          }}
        >
          <option value="">History</option>
          {history.map((item) => (
            <option key={item} value={item}>
              {truncate(item, 72)}
            </option>
          ))}
        </select>
      </div>
      <div className="buttonRow">
        <button className="primary" disabled={sendDisabled} onClick={() => void sendCommand()}>
          <SendHorizontal size={16} /> {pending ? "Sending..." : "Send"}
        </button>
        <button onClick={() => setCommandText("")}>
          <Eraser size={16} /> Clear
        </button>
      </div>
      <pre className="commandRecords">{records.join("\n")}</pre>
    </section>
  );
}

function readHistory(): string[] {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(historyStorageKey) || "[]");
    return Array.isArray(parsed) ? parsed.filter((item): item is string => typeof item === "string").slice(0, maxHistory) : [];
  } catch {
    return [];
  }
}

function truncate(value: string, limit: number): string {
  return value.length <= limit ? value : `${value.slice(0, limit)}...`;
}
