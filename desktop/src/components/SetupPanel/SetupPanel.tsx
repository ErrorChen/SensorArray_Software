import { Bluetooth, FileUp, RefreshCw, Rows3, Wifi, Zap } from "lucide-react";
import { useEffect, useMemo, useState as useSlot } from "react";

import type { BackendHttpClient } from "../../api/httpClient";
import type { BackendSnapshotPayload, BleDevice, SerialPort, SetupProfile, TransportMode, WifiDevice } from "../../api/types";
import { isCapacitanceMode } from "../../state/measurement";
import { isBleScanDisabled } from "../../state/transportUi";
import { MeasurementModeControl } from "./MeasurementModeControl";
import { RowModeProfileControl } from "./RowModeProfileControl";

type Props = {
  client: BackendHttpClient | null;
  snapshot: BackendSnapshotPayload | null;
  setupProfile: SetupProfile;
  onSetupProfileChange: (profile: SetupProfile) => void;
  onError: (message: string) => void;
};

const connectedStates = new Set(["connected", "streaming"]);
const busyStates = new Set(["connecting", "disconnecting", "reconnecting"]);
export const supportedRowOptions = Array.from({ length: 8 }, (_, index) => index + 1);

export function SetupPanel({ client, snapshot, setupProfile, onSetupProfileChange, onError }: Props): JSX.Element {
  const [transportMode, setTransportMode] = useSlot<TransportMode>("serial");
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
  const [serialScanStatus, setSerialScanStatus] = useSlot("");
  const [serialScanError, setSerialScanError] = useSlot("");
  const [bleScanning, setBleScanning] = useSlot(false);
  const [bleScanError, setBleScanError] = useSlot("");
  const [bleScanSummary, setBleScanSummary] = useSlot("");

  const connection = snapshot?.connection;
  const connectionMode = connection?.mode;
  const connectionState = connection?.state ?? "disconnected";
  const currentModeConnected = connectionMode === transportMode && connectedStates.has(connectionState);
  const currentModeBusy = connectionMode === transportMode && busyStates.has(connectionState);
  const bleScanDisabled = isBleScanDisabled(connectionMode, connectionState);
  const capacitanceMode = isCapacitanceMode(snapshot);
  const voltageAvailable = (snapshot?.frame.rowModes ?? snapshot?.matrix.modeByRow ?? []).slice(0, snapshot?.frame.rows ?? 8).includes("VOLT");

  useEffect(() => {
    setTransportMode(setupProfile.transport.mode);
    setSelectedPort(setupProfile.transport.serial.port || "");
    setBaud(setupProfile.transport.serial.baud);
    setSelectedBle(setupProfile.transport.ble.address || "");
    setSelectedWifi(setupProfile.transport.wifi.host || "");
    setFallbackHost(setupProfile.transport.wifi.fallbackHost || "");
    setReplayPath(setupProfile.transport.replay.path || "");
    setReplaySpeed(setupProfile.transport.replay.speed);
    setRows(setupProfile.acquisition.rows);
  }, [setupProfile]);

  useEffect(() => {
    if (!client) {
      return;
    }
    void run("Loading...", async () => {
      await client.setTransportMode(transportMode);
      if (transportMode === "serial") {
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

  async function handleTransportModeChange(nextMode: TransportMode): Promise<void> {
    setTransportMode(nextMode);
    updateTransportProfile({ mode: nextMode });
    if (!client) {
      return;
    }
    await client.setTransportMode(nextMode);
    if (nextMode === "serial") {
      await refreshSerialPorts();
    }
  }

  async function refreshSerialPorts(): Promise<void> {
    if (!client) {
      return;
    }
    setSerialScanStatus("Scanning serial ports...");
    setSerialScanError("");
    const response = await client.listSerialPorts();
    setSerialPorts(response.ports);
    if (!response.ok) {
      setSerialScanStatus("");
      setSerialScanError(response.error || "Serial port scan failed");
      return;
    }
    setSerialScanStatus(response.ports.length ? `Found ${response.ports.length} serial port${response.ports.length === 1 ? "" : "s"}` : "No serial ports found; enter a port manually.");
    const currentPort = selectedPort || setupProfile.transport.serial.port || "";
    if (!currentPort && response.ports.length === 1) {
      const port = response.ports[0].device;
      setSelectedPort(port);
      updateTransportProfile({ serial: { ...setupProfile.transport.serial, port } });
    }
  }

  async function scanBle(): Promise<void> {
    if (!client || bleScanDisabled) {
      return;
    }
    setBleScanning(true);
    setBleScanError("");
    setBleScanSummary("scanning");
    try {
      const response = await client.scanBle(10);
      const devices = mergeBleDevices(response.devices, response.advancedDevices);
      setBleDevices(devices);
      const verifiedDevices = devices.filter((device) => !device.advanced);
      const advancedDevices = devices.filter((device) => device.advanced);
      if (!response.ok || response.error) {
        setBleScanError(formatBleScanError(response.error || "BLE scan failed"));
      } else if (verifiedDevices.length === 0 && advancedDevices.length > 0) {
        setBleScanSummary("No verified SensorArray device found; enable Advanced devices to inspect all BLE candidates.");
      } else {
        setBleScanSummary(response.state || `found ${verifiedDevices.length} devices`);
      }
      const firstVerified = verifiedDevices.find((device) => device.verified) ?? verifiedDevices[0];
      const address = selectedBle || firstVerified?.address || "";
      setSelectedBle(address);
      if (address && !selectedBle) {
        updateTransportProfile({ ble: { ...setupProfile.transport.ble, address, deviceId: address } });
      }
    } catch (error) {
      setBleScanError(error instanceof Error ? error.message : String(error));
    } finally {
      setBleScanning(false);
    }
  }

  async function discoverWifi(): Promise<void> {
    if (!client) {
      return;
    }
    const devices = await client.discoverWifi();
    setWifiDevices(devices);
    const firstConfirmed = devices.find((device) => device.confirmed) ?? devices[0];
    const host = selectedWifi || firstConfirmed?.host || "";
    setSelectedWifi(host);
    if (host) {
      updateTransportProfile({ wifi: { ...setupProfile.transport.wifi, host } });
    }
  }

  async function primaryAction(): Promise<void> {
    if (!client) {
      return;
    }
    if (currentModeConnected) {
      await run("Disconnecting...", () => client.disconnect());
      return;
    }
    if (transportMode === "serial") {
      await run("Connecting...", () => client.connectSerial(selectedPort, baud, setupProfile.lifecycle.autoReconnect));
    } else if (transportMode === "ble") {
      await run("Connecting...", () => client.connectBle(selectedBle, selectedBle, setupProfile.lifecycle.autoReconnect));
    } else if (transportMode === "wifi") {
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
    onSetupProfileChange({ ...setupProfile, acquisition: { ...setupProfile.acquisition, rows: nextRows } });
    if (!client || rowsPending) {
      return;
    }
    setRowsPending(true);
    try {
      await client.setRows(nextRows);
    } catch (error) {
      setRows(previousRows);
      onSetupProfileChange({ ...setupProfile, acquisition: { ...setupProfile.acquisition, rows: previousRows } });
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
    if (transportMode === "serial") {
      return !selectedPort;
    }
    if (transportMode === "ble") {
      return !selectedBle || bleScanDisabled;
    }
    if (transportMode === "wifi") {
      return !(selectedWifi || fallbackHost);
    }
    return !replayPath;
  }

  return (
    <section className="setupPanel">
      <div className="panelHeader">Setup</div>
      <div className="panelHeader small">Transport</div>
      <div className="segmented">
        {(["serial", "ble", "wifi", "replay"] as TransportMode[]).map((item) => (
          <button key={item} className={transportMode === item ? "active" : ""} onClick={() => void run("Loading...", () => handleTransportModeChange(item))}>
            {item === "ble" ? "Bluetooth LE" : item === "wifi" ? "Wi-Fi UDP" : item[0].toUpperCase() + item.slice(1)}
          </button>
        ))}
      </div>

      {transportMode === "serial" ? (
        <div className="modePanel">
          <label>Port</label>
          <div className="inputRow">
            <input
              list="serial-port-options"
              value={selectedPort}
              onChange={(event) => {
                const port = event.target.value;
                setSelectedPort(port);
                updateTransportProfile({ serial: { ...setupProfile.transport.serial, port } });
              }}
              placeholder="Enter or select a serial port"
            />
            <datalist id="serial-port-options">
              {serialPorts.map((port) => (
                <option key={port.device} value={port.device} label={port.label} />
              ))}
            </datalist>
            <button title="Refresh ports" onClick={() => void run("Scanning...", refreshSerialPorts)}>
              <RefreshCw size={16} />
            </button>
          </div>
          {serialScanError ? <div className="inlineError compactMessage">{serialScanError}</div> : <div className="scanState">{serialScanStatus}</div>}
          <label>Baud</label>
          <input
            value={baud}
            type="number"
            min={1}
            onChange={(event) => {
              const nextBaud = Number(event.target.value);
              setBaud(nextBaud);
              updateTransportProfile({ serial: { ...setupProfile.transport.serial, baud: nextBaud } });
            }}
          />
        </div>
      ) : null}

      {transportMode === "ble" ? (
        <div className="modePanel">
          <label>Device</label>
          <select
            value={selectedBle}
            onChange={(event) => {
              const address = event.target.value;
              setSelectedBle(address);
              updateTransportProfile({ ble: { ...setupProfile.transport.ble, address, deviceId: address } });
            }}
          >
            <option value="">Select scanned BLE device</option>
            {visibleBleDevices.map((device) => (
              <option key={device.address || `${device.name}-${device.reason}-${device.matchReason}`} value={device.address}>
                {device.name || "Unnamed"} - {device.address} - {formatRssi(device.rssi)}
              </option>
            ))}
          </select>
          <label className="checkLine">
            <input type="checkbox" checked={showAdvancedBle} onChange={(event) => setShowAdvancedBle(event.target.checked)} />
            Advanced devices
          </label>
          <div className={bleScanError ? "inlineError compactMessage" : "scanState"}>
            {bleScanDisabled ? "BLE scan disabled while connected; disconnect first." : bleScanError || bleScanSummary || snapshot?.discovery.bleState || "idle"}
          </div>
          {visibleBleDevices.length ? (
            <div className="candidateList">
              {visibleBleDevices.map((device) => (
                <div key={device.address || `${device.name}-${device.reason}`} className="candidateRow">
                  <strong>{device.name || "Unnamed"}</strong>
                  <span>{device.address || "no address"}</span>
                  <span>{device.advanced ? device.reason || device.matchReason || "advanced candidate" : device.reason || device.matchReason || "verified candidate"}</span>
                </div>
              ))}
            </div>
          ) : null}
          <button disabled={bleScanDisabled || bleScanning || !client} onClick={() => void scanBle()}>
            <Bluetooth size={16} /> {bleScanning ? "Scanning..." : "Scan"}
          </button>
        </div>
      ) : null}

      {transportMode === "wifi" ? (
        <div className="modePanel">
          <label>Discovered host</label>
          <select
            value={selectedWifi}
            onChange={(event) => {
              const host = event.target.value;
              setSelectedWifi(host);
              updateTransportProfile({ wifi: { ...setupProfile.transport.wifi, host } });
            }}
          >
            <option value="">Select discovered host</option>
            {wifiDevices.map((device) => (
              <option key={`${device.host}-${device.method}`} value={device.host}>
                {device.host} - {device.method} - {device.confirmed ? "confirmed" : "unconfirmed"}
              </option>
            ))}
          </select>
          <label>Fallback host</label>
          <input
            value={fallbackHost}
            onChange={(event) => {
              const fallbackHost = event.target.value;
              setFallbackHost(fallbackHost);
              updateTransportProfile({ wifi: { ...setupProfile.transport.wifi, fallbackHost } });
            }}
          />
          <button onClick={() => void run("Discovering...", discoverWifi)}>
            <Wifi size={16} /> {busyAction === "Discovering..." ? "Discovering..." : "Discover"}
          </button>
        </div>
      ) : null}

      {transportMode === "replay" ? (
        <div className="modePanel">
          <label>Replay file</label>
          <div className="inputRow">
            <input
              value={replayPath}
              onChange={(event) => {
                const path = event.target.value;
                setReplayPath(path);
                updateTransportProfile({ replay: { ...setupProfile.transport.replay, path } });
              }}
            />
            <button
              title="Choose replay file"
              onClick={() =>
                void run("Loading...", async () => {
                  const path = await window.sensorarrayDesktop?.selectReplayFile();
                  if (path) {
                    setReplayPath(path);
                    updateTransportProfile({ replay: { ...setupProfile.transport.replay, path } });
                  }
                })
              }
            >
              <FileUp size={16} />
            </button>
          </div>
          <label>Speed</label>
          <input
            type="number"
            min={0.01}
            step={0.25}
            value={replaySpeed}
            onChange={(event) => {
              const speed = Number(event.target.value);
              setReplaySpeed(speed);
              updateTransportProfile({ replay: { ...setupProfile.transport.replay, speed } });
            }}
          />
        </div>
      ) : null}

      <button className="primary modePrimary" disabled={primaryDisabled()} onClick={() => void primaryAction()}>
        <Zap size={16} /> {busyAction ?? (currentModeConnected ? "Disconnect" : "Connect")}
      </button>

      <MeasurementModeControl
        client={client}
        snapshot={snapshot}
        setupProfile={setupProfile}
        onSetupProfileChange={onSetupProfileChange}
        onError={onError}
      />

      <RowModeProfileControl
        client={client}
        snapshot={snapshot}
        setupProfile={setupProfile}
        onSetupProfileChange={onSetupProfileChange}
        onError={onError}
      />

      <div className="controlGroup">
        <div className="panelHeader small">Rows</div>
        <div className="inputRow">
          <select disabled={!client || rowsPending} value={rows} onChange={(event) => void handleRowsChange(Number(event.target.value))}>
            {supportedRowOptions.map((value) => (
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
        <div className="panelHeader small">Lifecycle &amp; reconnect</div>
        <label className="checkLine">
          <input
            type="checkbox"
            checked={setupProfile.lifecycle.autoReconnect}
            onChange={(event) => updateLifecycleProfile({ autoReconnect: event.target.checked })}
          />
          Automatically reconnect the selected physical device
        </label>
        <label className="checkLine">
          <input
            type="checkbox"
            checked={setupProfile.lifecycle.resumeMeasurementAfterDeviceRestart}
            onChange={(event) => updateLifecycleProfile({ resumeMeasurementAfterDeviceRestart: event.target.checked })}
          />
          Resume measurement configuration after device restart
        </label>
        <label>Preferred USB stream after bootstrap</label>
        <select
          value={setupProfile.lifecycle.preferredUsbStream}
          onChange={(event) => updateLifecycleProfile({
            preferredUsbStream: event.target.value as SetupProfile["lifecycle"]["preferredUsbStream"]
          })}
        >
          <option value="DEVICE_DEFAULT">Device default</option>
          <option value="DEBUG">DEBUG</option>
          <option value="FULL">FULL</option>
        </select>
        <div className="modeOnlyNotice">FDC isolation, Recover, Restart, and ADS checks are never restored automatically.</div>
      </div>

      <div className="controlGroup">
        <div className="panelHeader small">Display</div>
        {capacitanceMode ? (
          <>
            <label>Capacitance display</label>
            <select
              disabled={!client}
              value={snapshot?.display.displayMode ?? "absolute_pf"}
              onChange={(event) =>
                void run("Saving...", async () => {
                  const displayMode = event.target.value as "absolute_pf" | "delta_percent";
                  onSetupProfileChange({ ...setupProfile, display: { ...setupProfile.display, displayMode } });
                  await client!.setDisplaySettings({ displayMode });
                })
              }
            >
              <option value="absolute_pf">Absolute C</option>
              <option value="delta_percent">Delta C/C0 %</option>
            </select>
            <div className="baselineStatus">{baselineMessage(snapshot)}</div>
          </>
        ) : (
          <div className="modeOnlyNotice">Baseline, Delta C/C0, and capacitance offsets are available for active CAP rows only.</div>
        )}
        {voltageAvailable ? (
          <>
            <label>Voltage display reference</label>
            <select
              disabled={!client}
              value={snapshot?.display.voltageReference ?? "vss_relative"}
              onChange={(event) =>
                void run("Saving...", async () => {
                  const voltageReference = event.target.value as "ground" | "vss_relative" | "rail_normalized";
                  onSetupProfileChange({ ...setupProfile, display: { ...setupProfile.display, voltageReference } });
                  await client!.setDisplaySettings({ voltageReference });
                })
              }
            >
              <option value="ground">Ground referenced</option>
              <option value="vss_relative">VSS-relative (recommended)</option>
              <option value="rail_normalized">Rail normalized (%)</option>
            </select>
            {snapshot?.display.voltageReference !== "ground" && !snapshot?.voltage?.derivedValid ? (
              <div className="modeOnlyNotice">Derived voltage is unavailable until a fresh, valid rail sample from the same boot is present.</div>
            ) : null}
          </>
        ) : null}
        <label className="checkLine">
          <input
            type="checkbox"
            checked={snapshot?.display.showCellText ?? true}
            onChange={(event) =>
              void run("Saving...", async () => {
                const showCellText = event.target.checked;
                onSetupProfileChange({ ...setupProfile, display: { ...setupProfile.display, showCellText } });
                await client!.setDisplaySettings({ showCellText });
              })
            }
          />
          Cell text
        </label>
        <label className="checkLine">
          <input
            type="checkbox"
            checked={snapshot?.display.pauseDisplay ?? false}
            onChange={(event) =>
              void run("Saving...", async () => {
                const pauseDisplay = event.target.checked;
                onSetupProfileChange({ ...setupProfile, display: { ...setupProfile.display, pauseDisplay } });
                await client!.setDisplaySettings({ pauseDisplay });
              })
            }
          />
          Pause display
        </label>
        <label className="checkLine">
          <input
            type="checkbox"
            checked={snapshot?.display.freezeColor ?? false}
            onChange={(event) =>
              void run("Saving...", async () => {
                const freezeColor = event.target.checked;
                onSetupProfileChange({ ...setupProfile, display: { ...setupProfile.display, freezeColor } });
                await client!.setDisplaySettings({ freezeColor });
              })
            }
          />
          Freeze colour
        </label>
        {capacitanceMode ? (
          <div className="buttonRow">
            <button disabled={!client || snapshot?.baseline.status === "capturing"} onClick={() => void run("Saving...", () => client!.baseline("capture").then(() => undefined))}>
              Set baseline
            </button>
            <button disabled={!client} onClick={() => void run("Saving...", () => client!.baseline("reset").then(() => undefined))}>
              Reset
            </button>
          </div>
        ) : null}
      </div>
    </section>
  );

  function updateTransportProfile(partial: Partial<SetupProfile["transport"]>): void {
    onSetupProfileChange({ ...setupProfile, transport: { ...setupProfile.transport, ...partial } });
  }

  function updateLifecycleProfile(partial: Partial<SetupProfile["lifecycle"]>): void {
    onSetupProfileChange({ ...setupProfile, lifecycle: { ...setupProfile.lifecycle, ...partial } });
  }
}

function formatRssi(rssi: number | null): string {
  return typeof rssi === "number" && Number.isFinite(rssi) ? `${rssi} dBm` : "RSSI unavailable";
}

function mergeBleDevices(devices: BleDevice[], advancedDevices: BleDevice[] | undefined): BleDevice[] {
  const byKey = new Map<string, BleDevice>();
  for (const device of [...devices, ...(advancedDevices ?? [])]) {
    const key = device.address || `${device.name}-${device.reason}-${device.matchReason}`;
    if (!byKey.has(key)) {
      byKey.set(key, device);
    }
  }
  return [...byKey.values()];
}

function formatBleScanError(error: string): string {
  return error.toLowerCase().includes("bleak unavailable") ? `BLE backend unavailable: ${error}` : error;
}

function rowsStatus(snapshot: BackendSnapshotPayload | null): string {
  const commands = snapshot?.commands as { requestedRows?: number; activeRows?: number; pendingRows?: number } | undefined;
  const requested = commands?.requestedRows;
  // A CRC-valid frame is the authoritative matrix geometry, including when a
  // Replay starts mid-session or a transport did not deliver historical RCMD.
  // Command state remains visible as requested/pending, but must not leave the
  // GUI claiming 8 rows while a valid 1/2/4-row frame is displayed.
  const active = snapshot?.frame.valid ? snapshot.frame.rows : commands?.activeRows ?? snapshot?.frame.rows;
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
