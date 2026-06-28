import { useCallback, useEffect, useMemo, useRef, useState as useSlot } from "react";
import type { PointerEvent as ReactPointerEvent, RefObject } from "react";

import { BackendHttpClient } from "./api/httpClient";
import { resolveBackendUrl } from "./api/backendUrl";
import { SnapshotWebSocket } from "./api/websocketClient";
import type { BackendSnapshotPayload, DesktopActionResult, HistoryPayload, SelectionSnapshot, WebSocketMessage } from "./api/types";
import { AdvancedPanel } from "./components/AdvancedPanel/AdvancedPanel";
import { CommandPanel } from "./components/CommandPanel/CommandPanel";
import { Heatmap } from "./components/Heatmap/Heatmap";
import { LogPanel } from "./components/LogPanel/LogPanel";
import { SetupPanel } from "./components/SetupPanel/SetupPanel";
import { StatusBar } from "./components/StatusBar/StatusBar";
import { TrendGrid } from "./components/TrendGrid/TrendGrid";
import { snapshotForDisplay } from "./state/appStore";
import { clampSplitRatio, type SplitLimits } from "./state/layout";
import { defaultSetupProfile, normaliseSetupProfile, setupProfileFromSnapshot } from "./state/setupProfile";
import { readStoredSetupProfile, writeStoredSetupProfile } from "./state/setupProfileStorage";
import type { SessionDataFormat, SetupProfile } from "./api/types";

const mainSplitKey = "sensorarray.layout.mainSplitRatio";
const bottomSplitKey = "sensorarray.layout.bottomSplitRatio";

export function App(): JSX.Element {
  const [backendUrl, setBackendUrl] = useSlot<string>("");
  const [snapshot, setSnapshot] = useSlot<BackendSnapshotPayload | null>(null);
  const [visualSnapshot, setVisualSnapshot] = useSlot<BackendSnapshotPayload | null>(null);
  const [history, setHistory] = useSlot<HistoryPayload | null>(null);
  const [optimisticSelection, setOptimisticSelection] = useSlot<SelectionSnapshot | null>(null);
  const [runtimeDirectory, setRuntimeDirectory] = useSlot(".");
  const [setupProfile, setSetupProfile] = useSlot<SetupProfile>(() => defaultSetupProfile("."));
  const [configMode, setConfigMode] = useSlot<"setup" | "advanced">("setup");
  const [socketState, setSocketState] = useSlot("disconnected");
  const [error, setError] = useSlot<string | null>(null);
  const [notice, setNotice] = useSlot<string | null>(null);
  const [mainSplitRatio, setMainSplitRatio] = usePersistentRatio(mainSplitKey, 0.75);
  const [bottomSplitRatio, setBottomSplitRatio] = usePersistentRatio(bottomSplitKey, 0.5);
  const mainSplitRef = useRef<HTMLDivElement | null>(null);
  const bottomSplitRef = useRef<HTMLDivElement | null>(null);
  const clientRef = useRef<BackendHttpClient | null>(null);
  const setupProfileRef = useRef<SetupProfile>(setupProfile);
  const snapshotRef = useRef<BackendSnapshotPayload | null>(snapshot);
  const runtimeDirectoryRef = useRef(runtimeDirectory);
  const latestSelectionRef = useRef<SelectionSnapshot | undefined>(undefined);
  const selectionRequestSeqRef = useRef(0);

  useEffect(() => {
    void resolveBackendUrl()
      .then((url) => {
        setBackendUrl(url);
        setError((current) => (isBackendError(current) ? null : current));
      })
      .catch((backendError) => setError(backendError instanceof Error ? backendError.message : String(backendError)));
    void (async () => {
      const runtime = (await window.sensorarrayDesktop?.getRuntimeDirectory()) || ".";
      setRuntimeDirectory(runtime);
      runtimeDirectoryRef.current = runtime;
      const storedProfile = readStoredSetupProfile(runtime);
      const desktopDirectory = await window.sensorarrayDesktop?.getDefaultSaveDirectory();
      const initialProfile = {
        ...storedProfile,
        paths: { ...storedProfile.paths, defaultSaveDirectory: storedProfile.paths.defaultSaveDirectory || desktopDirectory || runtime }
      };
      setSetupProfile(initialProfile);
      writeStoredSetupProfile(initialProfile);
      void window.sensorarrayDesktop?.setDefaultSaveDirectory(initialProfile.paths.defaultSaveDirectory);
    })();
  }, []);

  const client = useMemo(() => (backendUrl ? new BackendHttpClient(backendUrl) : null), [backendUrl]);

  useEffect(() => {
    clientRef.current = client;
  }, [client]);

  useEffect(() => {
    setupProfileRef.current = setupProfile;
  }, [setupProfile]);

  useEffect(() => {
    snapshotRef.current = snapshot;
  }, [snapshot]);

  useEffect(() => {
    runtimeDirectoryRef.current = runtimeDirectory;
  }, [runtimeDirectory]);

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
      onConnectionStatus: (status) => {
        setSocketState(status);
        if (status === "connected") {
          setError((current) => (isBackendError(current) ? null : current));
        }
      },
      onError: setError
    });
    socket.start();
    return () => socket.stop();
  }, [backendUrl, handleMessage]);

  const handleSetupProfileChange = useCallback((profile: SetupProfile) => {
    setupProfileRef.current = profile;
    setSetupProfile((current) => {
      if (current.paths.defaultSaveDirectory !== profile.paths.defaultSaveDirectory) {
        void window.sensorarrayDesktop?.setDefaultSaveDirectory(profile.paths.defaultSaveDirectory);
      }
      writeStoredSetupProfile(profile);
      return profile;
    });
  }, []);

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
    const exportSessionData = () => void handleExportSessionData(client);
    const importSessionData = () => void handleImportSessionData(client);
    const exportSetupProfile = () => void handleExportSetupProfile();
    const importSetupProfile = () => void handleImportSetupProfile(client);
    const removeImportSession = window.sensorarrayDesktop?.onImportSessionData(importSessionData);
    const removeExport = window.sensorarrayDesktop?.onExportSessionData(exportSessionData);
    const removeImportSetup = window.sensorarrayDesktop?.onImportSetupProfile(importSetupProfile);
    const removeExportSetup = window.sensorarrayDesktop?.onExportSetupProfile(exportSetupProfile);
    const removeScreenshot = window.sensorarrayDesktop?.onScreenshotResult?.(handleScreenshotResult);
    return () => {
      removeImport?.();
      removeImportSession?.();
      removeExport?.();
      removeImportSetup?.();
      removeExportSetup?.();
      removeScreenshot?.();
    };
  }, [client]);

  const selectCell = useCallback(async (cell: string) => {
    const requestSeq = selectionRequestSeqRef.current + 1;
    selectionRequestSeqRef.current = requestSeq;
    const optimistic = selectionFromCell(cell, latestSelectionRef.current);
    latestSelectionRef.current = optimistic;
    setOptimisticSelection(optimistic);
    try {
      await clientRef.current?.selectCell(cell);
      if (selectionRequestSeqRef.current !== requestSeq) {
        return;
      }
    } catch (selectError) {
      if (selectionRequestSeqRef.current === requestSeq) {
        setOptimisticSelection(null);
        setError(selectError instanceof Error ? selectError.message : String(selectError));
      }
    }
  }, []);

  async function handleExportSessionData(activeClient: BackendHttpClient): Promise<void> {
    try {
      setError(null);
      const defaultName = `sensorarray-session-${timestampForFilename(new Date())}.h5`;
      const chosen = await window.sensorarrayDesktop?.chooseSessionExportPath(defaultName);
      if (!chosen || chosen.canceled) {
        return;
      }
      if (!chosen.ok || !chosen.path) {
        throw new Error(chosen.error || "export path was not selected");
      }
      const format = sessionFormatFromPath(chosen.path);
      const payload = await activeClient.exportSession(format);
      const result = await window.sensorarrayDesktop?.writeBinaryFile(chosen.path, payload);
      if (!result?.ok) {
        throw new Error(result?.error || "export failed");
      }
      setNotice(`Exported session data: ${result.path}`);
    } catch (exportError) {
      setNotice(null);
      setError(exportError instanceof Error ? exportError.message : String(exportError));
    }
  }

  async function handleImportSessionData(activeClient: BackendHttpClient): Promise<void> {
    try {
      setError(null);
      const path = await window.sensorarrayDesktop?.selectSessionDataFile();
      if (!path) {
        return;
      }
      await activeClient.importSession(path);
      setNotice(`Imported session data: ${path}`);
    } catch (importError) {
      setNotice(null);
      setError(importError instanceof Error ? importError.message : String(importError));
    }
  }

  async function handleExportSetupProfile(): Promise<void> {
    try {
      setError(null);
      const profile = setupProfileFromSnapshot(snapshotRef.current, setupProfileRef.current);
      const defaultName = `sensorarray-setup-${timestampForFilename(new Date())}.json`;
      const result = await window.sensorarrayDesktop?.saveSetupProfile(defaultName, JSON.stringify(profile, null, 2));
      if (!result || result.canceled) {
        return;
      }
      if (!result.ok) {
        throw new Error(result.error || "setup export failed");
      }
      setNotice(`Exported setup profile: ${result.path}`);
    } catch (exportError) {
      setNotice(null);
      setError(exportError instanceof Error ? exportError.message : String(exportError));
    }
  }

  async function handleImportSetupProfile(activeClient: BackendHttpClient): Promise<void> {
    try {
      setError(null);
      const path = await window.sensorarrayDesktop?.selectSetupProfile();
      if (!path) {
        return;
      }
      const text = await window.sensorarrayDesktop?.readTextFile(path);
      const profile = normaliseSetupProfile(JSON.parse(text || "{}"), runtimeDirectoryRef.current);
      handleSetupProfileChange(profile);
      const directoryCheck = await window.sensorarrayDesktop?.setDefaultSaveDirectory(profile.paths.defaultSaveDirectory);
      const response = await activeClient.applySetupProfile(profile);
      const warnings = [...(response.warnings ?? [])];
      if (directoryCheck && !directoryCheck.ok) {
        warnings.push(directoryCheck.error || "Default save directory is not writable on this computer");
      }
      setNotice(warnings.length ? `Imported setup profile with warnings: ${warnings.join("; ")}` : `Imported setup profile: ${path}`);
    } catch (importError) {
      setNotice(null);
      setError(importError instanceof Error ? importError.message : String(importError));
    }
  }

  function handleScreenshotResult(result: DesktopActionResult): void {
    if (result.ok) {
      setError(null);
      setNotice(`Screenshot saved: ${result.path}`);
      return;
    }
    setNotice(null);
    setError(result.error || "screenshot failed");
  }

  const setFreezeColor = useCallback(
    async (freezeColor: boolean) => {
      try {
        const currentProfile = setupProfileRef.current;
        handleSetupProfileChange({ ...currentProfile, display: { ...currentProfile.display, freezeColor } });
        await client?.setDisplaySettings({ freezeColor });
      } catch (freezeError) {
        setError(freezeError instanceof Error ? freezeError.message : String(freezeError));
      }
    },
    [client, handleSetupProfileChange]
  );

  const visualWithOptimisticSelection = useMemo(() => {
    if (!visualSnapshot || !optimisticSelection) {
      return visualSnapshot;
    }
    return { ...visualSnapshot, selection: optimisticSelection };
  }, [optimisticSelection, visualSnapshot]);

  useEffect(() => {
    latestSelectionRef.current = visualWithOptimisticSelection?.selection ?? snapshot?.selection;
  }, [snapshot?.selection, visualWithOptimisticSelection?.selection]);

  const setLineEnding = useCallback(
    (lineEnding: SetupProfile["command"]["lineEnding"]) => {
      const currentProfile = setupProfileRef.current;
      handleSetupProfileChange({ ...currentProfile, command: { ...currentProfile.command, lineEnding } });
    },
    [handleSetupProfileChange]
  );

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
                <SetupPanel client={client} snapshot={snapshot} setupProfile={setupProfile} onSetupProfileChange={handleSetupProfileChange} onError={setError} />
              ) : (
                <AdvancedPanel
                  client={client}
                  snapshot={snapshot}
                  setupProfile={setupProfile}
                  runtimeDirectory={runtimeDirectory}
                  onSetupProfileChange={handleSetupProfileChange}
                  onError={setError}
                  onNotice={setNotice}
                />
              )}
            </section>
            <TrendGrid client={client} history={history} onHistory={setHistory} onError={setError} />
          </div>
        </div>
        <div ref={bottomSplitRef} className="bottomSplit">
          <div className="commandPane" style={{ flexBasis: `${bottomSplitRatio * 100}%` }}>
            <CommandPanel client={client} snapshot={snapshot} lineEnding={setupProfile.command.lineEnding} onLineEndingChange={setLineEnding} onError={setError} />
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

function isBackendError(value: string | null): boolean {
  return Boolean(value?.startsWith("Backend ") || value?.startsWith("Legacy unsafe backend port"));
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

function sessionFormatFromPath(filePath: string): SessionDataFormat {
  const extension = filePath.split(".").pop()?.toLowerCase();
  if (extension === "csv" || extension === "xlsx" || extension === "mat" || extension === "h5") {
    return extension;
  }
  throw new Error("Session export file extension must be .csv, .xlsx, .mat, or .h5");
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
