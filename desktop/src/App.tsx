import { useCallback, useEffect, useMemo, useRef, useState as useSlot } from "react";
import type { PointerEvent as ReactPointerEvent, RefObject } from "react";

import { BackendHttpClient } from "./api/httpClient";
import { SnapshotWebSocket } from "./api/websocketClient";
import type { BackendSnapshotPayload, HistoryPayload, WebSocketMessage } from "./api/types";
import { CommandPanel } from "./components/CommandPanel/CommandPanel";
import { Heatmap } from "./components/Heatmap/Heatmap";
import { LogPanel } from "./components/LogPanel/LogPanel";
import { SetupPanel } from "./components/SetupPanel/SetupPanel";
import { StatusBar } from "./components/StatusBar/StatusBar";
import { TrendGrid } from "./components/TrendGrid/TrendGrid";
import { snapshotForDisplay } from "./state/appStore";
import { clampSplitRatio, type SplitLimits } from "./state/layout";

const mainSplitKey = "sensorarray.layout.mainSplitRatio";
const bottomSplitKey = "sensorarray.layout.bottomSplitRatio";

export function App(): JSX.Element {
  const [backendUrl, setBackendUrl] = useSlot<string>("");
  const [snapshot, setSnapshot] = useSlot<BackendSnapshotPayload | null>(null);
  const [visualSnapshot, setVisualSnapshot] = useSlot<BackendSnapshotPayload | null>(null);
  const [history, setHistory] = useSlot<HistoryPayload | null>(null);
  const [socketState, setSocketState] = useSlot("disconnected");
  const [error, setError] = useSlot<string | null>(null);
  const [mainSplitRatio, setMainSplitRatio] = usePersistentRatio(mainSplitKey, 0.75);
  const [bottomSplitRatio, setBottomSplitRatio] = usePersistentRatio(bottomSplitKey, 0.5);
  const mainSplitRef = useRef<HTMLDivElement | null>(null);
  const bottomSplitRef = useRef<HTMLDivElement | null>(null);

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

  const setFreezeColor = useCallback(
    async (freezeColor: boolean) => {
      try {
        await client?.setDisplaySettings({ freezeColor });
      } catch (freezeError) {
        setError(freezeError instanceof Error ? freezeError.message : String(freezeError));
      }
    },
    [client]
  );

  return (
    <div className="appShell">
      <StatusBar snapshot={snapshot} socketState={socketState} />
      <main className="mainGrid">
        <div ref={mainSplitRef} className="workspaceSplit">
          <div className="heatmapPane" style={{ flexBasis: `${mainSplitRatio * 100}%` }}>
            <Heatmap snapshot={visualSnapshot} onSelectCell={selectCell} onSetFreezeColor={(value) => void setFreezeColor(value)} />
          </div>
          <div
            className="splitHandle"
            role="separator"
            aria-orientation="vertical"
            onPointerDown={(event) =>
              startSplitDrag(event, mainSplitRef, setMainSplitRatio, {
                minLeftRatio: 0.45,
                minRightPx: 300
              })
            }
          />
          <div className="rightPane">
            <SetupPanel client={client} snapshot={snapshot} onError={setError} />
            <TrendGrid history={history} />
          </div>
        </div>
        <div ref={bottomSplitRef} className="bottomSplit">
          <div className="commandPane" style={{ flexBasis: `${bottomSplitRatio * 100}%` }}>
            <CommandPanel client={client} snapshot={snapshot} onError={setError} />
          </div>
          <div
            className="splitHandle bottom"
            role="separator"
            aria-orientation="vertical"
            onPointerDown={(event) =>
              startSplitDrag(event, bottomSplitRef, setBottomSplitRatio, {
                minLeftPx: 260,
                minRightPx: 360
              })
            }
          />
          <div className="logPane">
            <LogPanel logs={snapshot?.logs ?? null} error={error} />
          </div>
        </div>
      </main>
    </div>
  );
}

function usePersistentRatio(key: string, defaultValue: number): [number, (nextRatio: number) => void] {
  const [ratio, setRatioState] = useSlot(() => {
    const stored = Number(window.localStorage.getItem(key));
    return Number.isFinite(stored) && stored > 0 && stored < 1 ? stored : defaultValue;
  });
  const setRatio = useCallback(
    (nextRatio: number) => {
      const rounded = Math.round(nextRatio * 10_000) / 10_000;
      setRatioState(rounded);
      window.localStorage.setItem(key, String(rounded));
      window.dispatchEvent(new Event("resize"));
    },
    [key]
  );
  return [ratio, setRatio];
}

function startSplitDrag(
  event: ReactPointerEvent<HTMLDivElement>,
  containerRef: RefObject<HTMLDivElement>,
  setRatio: (nextRatio: number) => void,
  limits: SplitLimits
): void {
  event.preventDefault();
  const updateRatio = (clientX: number) => {
    const rect = containerRef.current?.getBoundingClientRect();
    if (!rect || rect.width <= 0) {
      return;
    }
    const rawRatio = (clientX - rect.left) / rect.width;
    setRatio(clampSplitRatio(rawRatio, rect.width, limits));
  };
  const handlePointerMove = (moveEvent: PointerEvent) => updateRatio(moveEvent.clientX);
  const handlePointerUp = (upEvent: PointerEvent) => {
    updateRatio(upEvent.clientX);
    window.removeEventListener("pointermove", handlePointerMove);
    window.removeEventListener("pointerup", handlePointerUp);
  };
  updateRatio(event.clientX);
  window.addEventListener("pointermove", handlePointerMove);
  window.addEventListener("pointerup", handlePointerUp, { once: true });
}
