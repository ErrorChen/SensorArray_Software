import { useCallback, useEffect, useMemo, useState as useSlot } from "react";

import { BackendHttpClient } from "./api/httpClient";
import { SnapshotWebSocket } from "./api/websocketClient";
import type { BackendSnapshotPayload, HistoryPayload, WebSocketMessage } from "./api/types";
import { Heatmap } from "./components/Heatmap/Heatmap";
import { LogPanel } from "./components/LogPanel/LogPanel";
import { SetupPanel } from "./components/SetupPanel/SetupPanel";
import { StatusBar } from "./components/StatusBar/StatusBar";
import { TrendGrid } from "./components/TrendGrid/TrendGrid";
import { snapshotForDisplay } from "./state/appStore";

export function App(): JSX.Element {
  const [backendUrl, setBackendUrl] = useSlot<string>("");
  const [snapshot, setSnapshot] = useSlot<BackendSnapshotPayload | null>(null);
  const [visualSnapshot, setVisualSnapshot] = useSlot<BackendSnapshotPayload | null>(null);
  const [history, setHistory] = useSlot<HistoryPayload | null>(null);
  const [socketState, setSocketState] = useSlot("disconnected");
  const [error, setError] = useSlot<string | null>(null);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const queryUrl = params.get("backendUrl");
    if (queryUrl) {
      setBackendUrl(queryUrl);
      return;
    }
    void window.sensorarrayDesktop?.getBackendUrl().then((url) => setBackendUrl(url));
    if (!window.sensorarrayDesktop) {
      setBackendUrl("http://127.0.0.1:8765");
    }
  }, []);

  const client = useMemo(() => (backendUrl ? new BackendHttpClient(backendUrl) : null), [backendUrl]);

  const handleMessage = useCallback(
    (message: WebSocketMessage) => {
      if (message.type === "snapshot") {
        setSnapshot(message.payload);
        setVisualSnapshot((current) => snapshotForDisplay(message.payload, current));
      }
      if (message.type === "history") {
        setHistory(message.payload);
      }
      if (message.type === "error") {
        setError(`${message.scope}: ${message.message}`);
      }
    },
    [setSnapshot, setVisualSnapshot]
  );

  useEffect(() => {
    if (!backendUrl) {
      return;
    }
    const socket = new SnapshotWebSocket(backendUrl, {
      onMessage: handleMessage,
      onConnectionStatus: setSocketState
    });
    socket.start();
    return () => socket.stop();
  }, [backendUrl, handleMessage]);

  const selectCell = useCallback(
    async (cell: string) => {
      try {
        await client?.selectCell(cell);
      } catch (selectError) {
        setError(selectError instanceof Error ? selectError.message : String(selectError));
      }
    },
    [client]
  );

  return (
    <div className="appShell">
      <StatusBar snapshot={snapshot} socketState={socketState} />
      <main className="mainGrid">
        <Heatmap snapshot={visualSnapshot} onSelectCell={selectCell} />
        <div className="rightColumn">
          <SetupPanel client={client} snapshot={snapshot} onError={setError} />
          <TrendGrid history={history} />
        </div>
        <LogPanel logs={snapshot?.logs ?? null} error={error} />
      </main>
    </div>
  );
}
