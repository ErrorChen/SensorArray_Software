import { useCallback, useEffect, useMemo, useRef, useState as useSlot } from "react";
import type { PointerEvent as ReactPointerEvent, RefObject } from "react";

import { BackendHttpClient } from "./api/httpClient";
import { SnapshotWebSocket } from "./api/websocketClient";
import type { BackendSnapshotPayload, HistoryPayload, SelectionSnapshot, WebSocketMessage } from "./api/types";
import { AdvancedPanel } from "./components/AdvancedPanel/AdvancedPanel";
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
  const [optimisticSelection, setOptimisticSelection] = useSlot<SelectionSnapshot | null>(null);
  const [configMode, setConfigMode] = useSlot<"setup" | "advanced">("setup");
  const [socketState, setSocketState] = useSlot("disconnected");
  const [error, setError] = useSlot<string | null>(null);
  const [notice, setNotice] = useSlot<string | null>(null);
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
    if (!optimisticSelection || snapshot?.selection.title !== optimisticSelection.title) {
      return;
    }
    setOptimisticSelection(null);
  }, [optimisticSelection, setOptimisticSelection, snapshot?.selection.title]);

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

  useEffect(() => {
    if (!client) {
      return;
    }
    const removeImport = window.sensorarrayDesktop?.onImportReplayData((path) => {
      void (async () => {
        try {
          setError(null);
          setNotice(`Importing replay data: ${path}`);
          await client.openReplay(path, 1);
          await client.startReplay();
          setNotice(`Replay import started: ${path}`);
        } catch (importError) {
          setNotice(null);
          setError(importError instanceof Error ? importError.message : String(importError));
        }
      })();
    });
    const removeExport = window.sensorarrayDesktop?.onExportSessionData(() => {
      void (async () => {
        try {
          setError(null);
          const payload = await client.exportSession();
          const defaultName = `sensorarray-session-${timestampForFilename(new Date())}.json`;
          const result = await window.sensorarrayDesktop?.saveExportedSession(defaultName, JSON.stringify(payload, null, 2));
          if (!result || result.canceled) {
            return;
          }
          if (!result.ok) {
            throw new Error(result.error || "export failed");
          }
          setNotice(`Exported session data: ${result.path}`);
        } catch (exportError) {
          setNotice(null);
          setError(exportError instanceof Error ? exportError.message : String(exportError));
        }
      })();
    });
    return () => {
      removeImport?.();
      removeExport?.();
    };
  }, [client]);

  const selectCell = useCallback(
    async (cell: string) => {
      setOptimisticSelection(selectionFromCell(cell, snapshot?.selection));
      try {
        await client?.selectCell(cell);
      } catch (selectError) {
        setOptimisticSelection(null);
        setError(selectError instanceof Error ? selectError.message : String(selectError));
      }
    },
    [client, setOptimisticSelection, snapshot?.selection]
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

  const visualWithOptimisticSelection = useMemo(() => {
    if (!visualSnapshot || !optimisticSelection) {
      return visualSnapshot;
    }
    return { ...visualSnapshot, selection: optimisticSelection };
  }, [optimisticSelection, visualSnapshot]);

  return (
    <div className="appShell">
      <StatusBar snapshot={snapshot} socketState={socketState} />
      <main className="mainGrid">
        <div ref={mainSplitRef} className="workspaceSplit">
          <div className="heatmapPane" style={{ flexBasis: `${mainSplitRatio * 100}%` }}>
            <Heatmap snapshot={visualWithOptimisticSelection} onSelectCell={selectCell} onSetFreezeColor={(value) => void setFreezeColor(value)} />
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
            <section className="configPane">
              <div className="topLevelTabs" role="tablist" aria-label="Configuration panels">
                <button className={configMode === "setup" ? "active" : ""} role="tab" aria-selected={configMode === "setup"} onClick={() => setConfigMode("setup")}>
                  Setup
                </button>
                <button
                  className={configMode === "advanced" ? "active" : ""}
                  role="tab"
                  aria-selected={configMode === "advanced"}
                  onClick={() => setConfigMode("advanced")}
                >
                  Advanced
                </button>
              </div>
              {configMode === "setup" ? (
                <SetupPanel client={client} snapshot={snapshot} onError={setError} />
              ) : (
                <AdvancedPanel client={client} snapshot={snapshot} onError={setError} />
              )}
            </section>
            <TrendGrid client={client} history={history} onHistory={setHistory} onError={setError} />
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
            <LogPanel logs={snapshot?.logs ?? null} error={error} notice={notice} />
          </div>
        </div>
      </main>
    </div>
  );
}

function selectionFromCell(cell: string, current: SelectionSnapshot | undefined): SelectionSnapshot {
  const match = /^S(\d+)D(\d+)$/i.exec(cell);
  if (!match) {
    return (
      current ?? {
        rowIndex: 0,
        rowLabel: "S1",
        fdcGroup: "primary",
        detectorStart: 1,
        detectorEnd: 4,
        cells: ["S1D1", "S1D2", "S1D3", "S1D4"],
        title: "S1 Primary FDC D1-D4",
        selectionRevision: 0
      }
    );
  }
  const row = Number(match[1]);
  const detector = Number(match[2]);
  const primary = detector <= 4;
  const start = primary ? 1 : 5;
  const end = primary ? 4 : 8;
  const rowLabel = `S${row}`;
  const cells = Array.from({ length: end - start + 1 }, (_, index) => `${rowLabel}D${start + index}`);
  const title = `${rowLabel} ${primary ? "Primary" : "Secondary"} FDC D${start}-D${end}`;
  return {
    rowIndex: row - 1,
    rowLabel,
    fdcGroup: primary ? "primary" : "secondary",
    detectorStart: start,
    detectorEnd: end,
    cells,
    title,
    selectionRevision: (current?.selectionRevision ?? 0) + 1
  };
}

function timestampForFilename(date: Date): string {
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}-${pad(date.getHours())}${pad(date.getMinutes())}${pad(
    date.getSeconds()
  )}`;
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
