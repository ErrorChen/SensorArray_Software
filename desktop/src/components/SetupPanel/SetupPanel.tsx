import { Bluetooth, FileUp, RefreshCw, Rows3, Wifi, Zap } from "lucide-react";
import { useEffect, useMemo, useState as useSlot } from "react";

import type { BackendHttpClient } from "../../api/httpClient";
import type { BackendSnapshotPayload, BleDevice, SerialPort, TransportMode, WifiDevice } from "../../api/types";
import { isBleScanDisabled } from "../../state/transportUi";

type Props = {
  client: BackendHttpClient | null;
  snapshot: BackendSnapshotPayload | null;
  onError: (message: string) => void;
};

const connectedStates = new Set(["connected", "streaming"]);
const busyStates = new Set(["connecting", "disconnecting", "reconnecting"]);

export function SetupPanel({ client, snapshot, onError }: Props): JSX.Element {
  const [mode, setMode] = useSlot<TransportMode>("serial");
  const [serialPorts, setSerialPorts] = useSlot<SerialPort[]>([]);
  const [selectedPort, setSelectedPort] = useSlot("");
  const [baud, setBaud] = useSlot(115200);
  const [bleDevices, setBleDevices] = useSlot<BleDevice[]>([]);
  const [selectedBle, setSelectedBle] = useSlot("");
  const [showAdvancedBle, setShowAdvancedBle] = useSlot(false);
  const [wifiDevices, setWifiDevices] = useSlot<WifiDevice[]>([]);
  const [selectedWifi, setSelectedWifi] = useSlot("");
  const [fallbackHost, setFallbackHost] = useSlot("192.168.4.1");
  const [replayPath, setReplayPath] = useSlot("");
  const [replaySpeed, setReplaySpeed] = useSlot(1);
  const [rows, setRows] = useSlot(8);
  const [rowsPending, setRowsPending] = useSlot(false);
  const [busyAction, setBusyAction] = useSlot<string | null>(null);

  const connection = snapshot?.connection;
  const connectionMode = connection?.mode;
  const connectionState = connection?.state ?? "disconnected";
  const currentModeConnected = connectionMode === mode && connectedStates.has(connectionState);
  const currentModeBusy = connectionMode === mode && busyStates.has(connectionState);
  const bleScanDisabled = isBleScanDisabled(connectionMode, connectionState);

  useEffect(() => {
    if (!client) {
      return;
    }
    void run("Loading...", async () => {
      await client.setMode(mode);
      if (mode === "serial") {
        await refreshSerialPorts();
      }
    });
  }, [client]);

  useEffect(() => {
    if (!rowsPending && typeof snapshot?.frame.rows === "number") {
      setRows(snapshot.frame.rows);
    }
  }, [rowsPending, setRows, snapshot?.frame.rows]);

  const visibleBleDevices = useMemo(
    () => bleDevices.filter((device) => showAdvancedBle || !device.advanced),
    [bleDevices, showAdvancedBle]
  );

  async function run(label: string, action: () => Promise<void>): Promise<void> {
    try {
      setBusyAction(label);
      await action();
    } catch (error) {
      onError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusyAction(null);
    }
  }

  async function handleModeChange(nextMode: TransportMode): Promise<void> {
    setMode(nextMode);
    if (!client) {
      return;
    }
    await client.setMode(nextMode);
    if (nextMode === "serial") {
      await refreshSerialPorts();
    }
  }

  async function refreshSerialPorts(): Promise<void> {
    if (!client) {
      return;
    }
    const ports = await client.listSerialPorts();
    setSerialPorts(ports);
    if (!selectedPort && ports.length === 1) {
      setSelectedPort(ports[0].device);
    }
  }

  async function scanBle(): Promise<void> {
    if (!client || bleScanDisabled) {
      return;
    }
    const devices = await client.scanBle();
    setBleDevices(devices);
    const firstVerified = devices.find((device) => device.verified || !device.advanced);
    setSelectedBle((current) => current || firstVerified?.address || "");
  }

  async function discoverWifi(): Promise<void> {
    if (!client) {
      return;
    }
    const devices = await client.discoverWifi();
    setWifiDevices(devices);
    const firstConfirmed = devices.find((device) => device.confirmed) ?? devices[0];
    setSelectedWifi((current) => current || firstConfirmed?.host || "");
  }

  async function primaryAction(): Promise<void> {
    if (!client) {
      return;
    }
    if (currentModeConnected) {
      await run("Disconnecting...", () => client.disconnect());
      return;
    }
    if (mode === "serial") {
      await run("Connecting...", () => client.connectSerial(selectedPort, baud));
    } else if (mode === "ble") {
      await run("Connecting...", () => client.connectBle(selectedBle, selectedBle));
    } else if (mode === "wifi") {
      await run("Connecting...", () => client.connectWifi(selectedWifi || fallbackHost));
    } else {
      await run("Connecting...", async () => {
        await client.openReplay(replayPath, replaySpeed);
        await client.startReplay();
      });
    }
  }

  async function handleRowsChange(nextRows: number): Promise<void> {
    const previousRows = snapshot?.frame.rows ?? rows;
    setRows(nextRows);
    if (!client || rowsPending) {
      return;
    }
    setRowsPending(true);
    try {
      await client.setRows(nextRows);
    } catch (error) {
      setRows(previousRows);
      onError(error instanceof Error ? error.message : String(error));
    } finally {
      setRowsPending(false);
    }
  }

  function primaryDisabled(): boolean {
    if (!client || busyAction !== null || currentModeBusy) {
      return true;
    }
    if (currentModeConnected) {
      return false;
    }
    if (mode === "serial") {
      return !selectedPort;
    }
    if (mode === "ble") {
      return !selectedBle || bleScanDisabled;
    }
    if (mode === "wifi") {
      return !(selectedWifi || fallbackHost);
    }
    return !replayPath;
  }

  return (
    <section className="setupPanel">
      <div className="panelHeader">Setup</div>
      <div className="segmented">
        {(["serial", "ble", "wifi", "replay"] as TransportMode[]).map((item) => (
          <button key={item} className={mode === item ? "active" : ""} onClick={() => void run("Loading...", () => handleModeChange(item))}>
            {item === "ble" ? "Bluetooth LE" : item === "wifi" ? "Wi-Fi UDP" : item[0].toUpperCase() + item.slice(1)}
          </button>
        ))}
      </div>

      {mode === "serial" ? (
        <div className="modePanel">
          <label>Port</label>
          <div className="inputRow">
            <select value={selectedPort} onChange={(event) => setSelectedPort(event.target.value)}>
              <option value="">Select scanned port</option>
              {serialPorts.map((port) => (
                <option key={port.device} value={port.device}>
                  {port.label}
                </option>
              ))}
            </select>
            <button title="Refresh ports" onClick={() => void run("Scanning...", refreshSerialPorts)}>
              <RefreshCw size={16} />
            </button>
          </div>
          <label>Baud</label>
          <input value={baud} type="number" min={1} onChange={(event) => setBaud(Number(event.target.value))} />
        </div>
      ) : null}

      {mode === "ble" ? (
        <div className="modePanel">
          <label>Device</label>
          <select value={selectedBle} onChange={(event) => setSelectedBle(event.target.value)}>
            <option value="">Select scanned BLE device</option>
            {visibleBleDevices.map((device) => (
              <option key={device.address} value={device.address}>
                {device.name || "Unnamed"} - {device.address} - {formatRssi(device.rssi)}
              </option>
            ))}
          </select>
          <label className="checkLine">
            <input type="checkbox" checked={showAdvancedBle} onChange={(event) => setShowAdvancedBle(event.target.checked)} />
            Advanced devices
          </label>
          <div className="scanState">{bleScanDisabled ? "BLE scan disabled while connected" : snapshot?.discovery.bleState ?? "idle"}</div>
          <button disabled={bleScanDisabled || busyAction !== null} onClick={() => void run("Scanning...", scanBle)}>
            <Bluetooth size={16} /> {busyAction === "Scanning..." ? "Scanning..." : "Scan"}
          </button>
        </div>
      ) : null}

      {mode === "wifi" ? (
        <div className="modePanel">
          <label>Discovered host</label>
          <select value={selectedWifi} onChange={(event) => setSelectedWifi(event.target.value)}>
            <option value="">Select discovered host</option>
            {wifiDevices.map((device) => (
              <option key={`${device.host}-${device.method}`} value={device.host}>
                {device.host} - {device.method} - {device.confirmed ? "confirmed" : "unconfirmed"}
              </option>
            ))}
          </select>
          <label>Fallback host</label>
          <input value={fallbackHost} onChange={(event) => setFallbackHost(event.target.value)} />
          <button onClick={() => void run("Discovering...", discoverWifi)}>
            <Wifi size={16} /> {busyAction === "Discovering..." ? "Discovering..." : "Discover"}
          </button>
        </div>
      ) : null}

      {mode === "replay" ? (
        <div className="modePanel">
          <label>Replay file</label>
          <div className="inputRow">
            <input value={replayPath} onChange={(event) => setReplayPath(event.target.value)} />
            <button
              title="Choose replay file"
              onClick={() =>
                void run("Loading...", async () => {
                  const path = await window.sensorarrayDesktop?.selectReplayFile();
                  if (path) setReplayPath(path);
                })
              }
            >
              <FileUp size={16} />
            </button>
          </div>
          <label>Speed</label>
          <input type="number" min={0.01} step={0.25} value={replaySpeed} onChange={(event) => setReplaySpeed(Number(event.target.value))} />
        </div>
      ) : null}

      <button className="primary modePrimary" disabled={primaryDisabled()} onClick={() => void primaryAction()}>
        <Zap size={16} /> {busyAction ?? (currentModeConnected ? "Disconnect" : "Connect")}
      </button>

      <div className="controlGroup">
        <div className="panelHeader small">Rows</div>
        <div className="inputRow">
          <select disabled={!client || rowsPending} value={rows} onChange={(event) => void handleRowsChange(Number(event.target.value))}>
            {[1, 2, 3, 4, 5, 6, 7, 8].map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
          <span className="rowsStatus">
            <Rows3 size={15} /> {rowsPending ? "Applying rows..." : rowsStatus(snapshot)}
          </span>
        </div>
      </div>

      <div className="controlGroup">
        <div className="panelHeader small">Display</div>
        <label>Mode</label>
        <select
          disabled={!client}
          value={snapshot?.display.displayMode ?? "absolute_pf"}
          onChange={(event) =>
            void run("Saving...", () => client!.setDisplaySettings({ displayMode: event.target.value as "absolute_pf" | "delta_percent" }).then(() => undefined))
          }
        >
          <option value="absolute_pf">Absolute C</option>
          <option value="delta_percent">Delta C/C0 %</option>
        </select>
        <div className="baselineStatus">{baselineMessage(snapshot)}</div>
        <label className="checkLine">
          <input
            type="checkbox"
            checked={snapshot?.display.showCellText ?? true}
            onChange={(event) => void run("Saving...", () => client!.setDisplaySettings({ showCellText: event.target.checked }).then(() => undefined))}
          />
          Cell text
        </label>
        <label className="checkLine">
          <input
            type="checkbox"
            checked={snapshot?.display.pauseDisplay ?? false}
            onChange={(event) => void run("Saving...", () => client!.setDisplaySettings({ pauseDisplay: event.target.checked }).then(() => undefined))}
          />
          Pause display
        </label>
        <label className="checkLine">
          <input
            type="checkbox"
            checked={snapshot?.display.freezeColor ?? false}
            onChange={(event) => void run("Saving...", () => client!.setDisplaySettings({ freezeColor: event.target.checked }).then(() => undefined))}
          />
          Freeze colour
        </label>
        <div className="buttonRow">
          <button disabled={!client || snapshot?.baseline.status === "capturing"} onClick={() => void run("Saving...", () => client!.baseline("capture").then(() => undefined))}>
            Set baseline
          </button>
          <button disabled={!client} onClick={() => void run("Saving...", () => client!.baseline("reset").then(() => undefined))}>
            Reset
          </button>
        </div>
      </div>
    </section>
  );
}

function formatRssi(rssi: number | null): string {
  return typeof rssi === "number" && Number.isFinite(rssi) ? `${rssi} dBm` : "RSSI unavailable";
}

function rowsStatus(snapshot: BackendSnapshotPayload | null): string {
  const commands = snapshot?.commands as { requestedRows?: number; activeRows?: number; pendingRows?: number } | undefined;
  const requested = commands?.requestedRows;
  const active = commands?.activeRows ?? snapshot?.frame.rows;
  const pending = commands?.pendingRows;
  if (typeof pending === "number") {
    return `requested ${pending}; applied ${active ?? "-"}`;
  }
  if (typeof requested === "number") {
    return `requested ${requested}; applied ${active ?? "-"}`;
  }
  return `applied ${active ?? "-"}`;
}

function baselineMessage(snapshot: BackendSnapshotPayload | null): string {
  const baseline = snapshot?.baseline;
  if (!snapshot?.frame.valid && baseline?.status !== "capturing") {
    return "No data";
  }
  if (baseline?.status === "capturing") {
    const progress = typeof baseline.progress === "number" ? ` ${Math.round(baseline.progress * 100)}%` : "";
    return `Capturing baseline...${progress}`;
  }
  if (baseline?.status === "ready" || baseline?.ready) {
    return `Ready (${baseline.validCells ?? 0} cells)`;
  }
  if (baseline?.status === "invalid") {
    return `Invalid: ${baseline.invalidReason || "baseline invalid"}`;
  }
  if (baseline?.status === "no_data") {
    return "No data";
  }
  if (baseline?.pendingDisplayMode === "delta_percent" || snapshot?.display.pendingDisplayMode === "delta_percent") {
    return "Delta pending baseline";
  }
  return baseline?.label || "Not captured";
}
