import { Bluetooth, FileUp, Pause, Play, RefreshCw, Rows3, Unplug, Wifi, Zap } from "lucide-react";
import { useEffect, useMemo, useState as useSlot } from "react";

import type { BackendHttpClient } from "../../api/httpClient";
import type { BackendSnapshotPayload, BleDevice, SerialPort, TransportMode, WifiDevice } from "../../api/types";

type Props = {
  client: BackendHttpClient | null;
  snapshot: BackendSnapshotPayload | null;
  onError: (message: string) => void;
};

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

  useEffect(() => {
    if (!client) {
      return;
    }
    void handleModeChange(mode);
  }, [client]);

  const visibleBleDevices = useMemo(
    () => bleDevices.filter((device) => showAdvancedBle || !device.advanced),
    [bleDevices, showAdvancedBle]
  );

  async function run(action: () => Promise<void>): Promise<void> {
    try {
      await action();
    } catch (error) {
      onError(error instanceof Error ? error.message : String(error));
    }
  }

  async function handleModeChange(nextMode: TransportMode): Promise<void> {
    setMode(nextMode);
    if (!client) {
      return;
    }
    await client.setMode(nextMode);
    if (nextMode === "serial") {
      const ports = await client.listSerialPorts();
      setSerialPorts(ports);
      if (ports.length === 1) {
        setSelectedPort(ports[0].device);
      }
    }
    if (nextMode === "ble") {
      const devices = await client.scanBle();
      setBleDevices(devices);
      const firstVerified = devices.find((device) => device.verified || !device.advanced);
      setSelectedBle(firstVerified?.address ?? "");
    }
    if (nextMode === "wifi") {
      const devices = await client.discoverWifi();
      setWifiDevices(devices);
      const firstConfirmed = devices.find((device) => device.confirmed) ?? devices[0];
      setSelectedWifi(firstConfirmed?.host ?? "");
    }
  }

  return (
    <section className="setupPanel">
      <div className="panelHeader">Setup</div>
      <div className="segmented">
        {(["serial", "ble", "wifi", "replay"] as TransportMode[]).map((item) => (
          <button key={item} className={mode === item ? "active" : ""} onClick={() => void run(() => handleModeChange(item))}>
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
            <button title="Refresh ports" onClick={() => void run(async () => setSerialPorts(await client!.listSerialPorts()))}>
              <RefreshCw size={16} />
            </button>
          </div>
          <label>Baud</label>
          <input value={baud} type="number" min={1} onChange={(event) => setBaud(Number(event.target.value))} />
          <button className="primary" onClick={() => void run(() => client!.connectSerial(selectedPort, baud))}>
            <Zap size={16} /> Connect
          </button>
        </div>
      ) : null}

      {mode === "ble" ? (
        <div className="modePanel">
          <label>Device</label>
          <select value={selectedBle} onChange={(event) => setSelectedBle(event.target.value)}>
            <option value="">Select scanned BLE device</option>
            {visibleBleDevices.map((device) => (
              <option key={device.address} value={device.address}>
                {(device.name || "Unnamed")} · {device.address} · {device.rssi ?? "?"} dBm
              </option>
            ))}
          </select>
          <label className="checkLine">
            <input type="checkbox" checked={showAdvancedBle} onChange={(event) => setShowAdvancedBle(event.target.checked)} />
            Advanced devices
          </label>
          <div className="scanState">{snapshot?.discovery.bleState ?? "idle"}</div>
          <div className="buttonRow">
            <button onClick={() => void run(async () => setBleDevices(await client!.scanBle()))}>
              <Bluetooth size={16} /> Scan
            </button>
            <button className="primary" onClick={() => void run(() => client!.connectBle(selectedBle, selectedBle))}>
              <Zap size={16} /> Connect
            </button>
          </div>
        </div>
      ) : null}

      {mode === "wifi" ? (
        <div className="modePanel">
          <label>Discovered host</label>
          <select value={selectedWifi} onChange={(event) => setSelectedWifi(event.target.value)}>
            <option value="">Select discovered host</option>
            {wifiDevices.map((device) => (
              <option key={`${device.host}-${device.method}`} value={device.host}>
                {device.host} · {device.method} · {device.confirmed ? "confirmed" : "unconfirmed"}
              </option>
            ))}
          </select>
          <label>Fallback host</label>
          <input value={fallbackHost} onChange={(event) => setFallbackHost(event.target.value)} />
          <div className="buttonRow">
            <button onClick={() => void run(async () => setWifiDevices(await client!.discoverWifi()))}>
              <Wifi size={16} /> Discover
            </button>
            <button className="primary" onClick={() => void run(() => client!.connectWifi(selectedWifi || fallbackHost))}>
              <Zap size={16} /> Connect
            </button>
          </div>
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
                void run(async () => {
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
          <div className="buttonRow">
            <button
              className="primary"
              onClick={() =>
                void run(async () => {
                  await client!.openReplay(replayPath, replaySpeed);
                  await client!.startReplay();
                })
              }
            >
              <Play size={16} /> Start
            </button>
            <button onClick={() => void run(() => client!.stopReplay())}>
              <Pause size={16} /> Stop
            </button>
          </div>
        </div>
      ) : null}

      <div className="controlGroup">
        <div className="panelHeader small">Rows</div>
        <div className="inputRow">
          <select value={rows} onChange={(event) => setRows(Number(event.target.value))}>
            {[1, 2, 3, 4, 5, 6, 7, 8].map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
          <button onClick={() => void run(() => client!.setRows(rows))}>
            <Rows3 size={16} /> Apply
          </button>
        </div>
      </div>

      <div className="controlGroup">
        <div className="panelHeader small">Display</div>
        <label>Mode</label>
        <select
          value={snapshot?.display.displayMode ?? "absolute_pf"}
          onChange={(event) => void run(() => client!.setDisplaySettings({ displayMode: event.target.value as "absolute_pf" | "delta_percent" }))}
        >
          <option value="absolute_pf">Absolute C</option>
          <option value="delta_percent">Delta C/C0 %</option>
        </select>
        <label className="checkLine">
          <input
            type="checkbox"
            checked={snapshot?.display.showCellText ?? true}
            onChange={(event) => void run(() => client!.setDisplaySettings({ showCellText: event.target.checked }))}
          />
          Cell text
        </label>
        <label className="checkLine">
          <input
            type="checkbox"
            checked={snapshot?.display.pauseDisplay ?? false}
            onChange={(event) => void run(() => client!.setDisplaySettings({ pauseDisplay: event.target.checked }))}
          />
          Pause display
        </label>
        <label className="checkLine">
          <input
            type="checkbox"
            checked={snapshot?.display.freezeColor ?? false}
            onChange={(event) => void run(() => client!.setDisplaySettings({ freezeColor: event.target.checked }))}
          />
          Freeze colour
        </label>
        <div className="buttonRow">
          <button onClick={() => void run(() => client!.baseline("capture"))}>Set baseline</button>
          <button onClick={() => void run(() => client!.baseline("reset"))}>Reset</button>
        </div>
      </div>

      <button className="disconnect" onClick={() => void run(() => client!.disconnect())}>
        <Unplug size={16} /> Disconnect
      </button>
    </section>
  );
}
