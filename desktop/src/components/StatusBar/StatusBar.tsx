import { Activity, CircleAlert, RadioTower } from "lucide-react";

import type { BackendSnapshotPayload } from "../../api/types";
import { appliedMeasurementMode, pendingMeasurementMode } from "../../state/measurement";

type Props = {
  snapshot: BackendSnapshotPayload | null;
  socketState: string;
};

export function StatusBar({ snapshot, socketState }: Props): JSX.Element {
  const connection = snapshot?.connection;
  const frame = snapshot?.frame;
  const error = connection?.error;
  const appliedMode = appliedMeasurementMode(snapshot);
  const pendingMode = pendingMeasurementMode(snapshot);
  const adsUnconfirmed = isAdsIdentityUnconfirmed(snapshot);
  const battery = batteryStatus(snapshot);
  return (
    <header className="statusBar">
      <div className="brand">SensorArray</div>
      <div className="statusItems">
        <span className="statusItem">
          <RadioTower size={16} /> Transport: {connection?.mode ?? "serial"} / {connection?.state ?? "disconnected"}
        </span>
        <span className="statusItem">{connection?.deviceLabel || "No device"}</span>
        <span className="statusItem">
          <Activity size={16} /> Measurement: {appliedMode}
          {pendingMode ? ` → ${pendingMode} (${snapshot?.measurement.transitionState ?? "requested"})` : ""}
        </span>
        <span className="statusItem">seq {frame?.seq ?? "-"}</span>
        {rateItems(snapshot).map((item) => (
          <span key={item.label} className="statusItem">
            {item.label} {item.value.toFixed(1)} fps
          </span>
        ))}
        {!snapshot?.rates && typeof frame?.fps === "number" ? <span className="statusItem">Host frames {frame.fps.toFixed(1)} fps</span> : null}
        {battery ? <span className="statusItem">{battery}</span> : null}
        {adsUnconfirmed ? <span className="statusWarning">ADS identity unconfirmed</span> : null}
        <span className={`socketState ${socketState}`}>{socketState}</span>
      </div>
      {error ? (
        <div className="statusError" title={error}>
          <CircleAlert size={16} /> {error}
        </div>
      ) : null}
    </header>
  );
}

function rateItems(snapshot: BackendSnapshotPayload | null): Array<{ label: string; value: number }> {
  const rates = snapshot?.rates;
  if (!rates) {
    return [];
  }
  const candidates: Array<[string, number | null | undefined]> = [
    ["Capture", rates.captureFps],
    ["Emitted", rates.emittedFps],
    ["Serial", rates.serialOutputFps],
    ["BLE", rates.bleOutputFps],
    ["Wi-Fi", rates.wifiOutputFps],
    ["Target", rates.targetFps],
    ["Host parse", rates.hostParserFps]
  ];
  return candidates
    .filter((item): item is [string, number] => typeof item[1] === "number" && Number.isFinite(item[1]))
    .map(([label, value]) => ({ label, value }));
}

function isAdsIdentityUnconfirmed(snapshot: BackendSnapshotPayload | null): boolean {
  const ads = snapshot?.ads;
  if (!ads || ads.identityAvailable === false || ads.identityConfirmed === null) {
    return false;
  }
  if (ads.identityConfirmed === false || ads.label === "ADS identity unconfirmed") {
    return true;
  }
  const chip = String(ads.identity?.chip ?? ads.chip ?? "").trim().toLowerCase();
  const valid = ads.identity?.valid ?? ads.valid;
  return (valid === false || valid === 0 || valid === "0") && (!chip || chip === "unknown");
}

function batteryStatus(snapshot: BackendSnapshotPayload | null): string {
  const battery = snapshot?.battery;
  if (
    !battery ||
    battery.available === false ||
    (String(battery.state ?? "").toLowerCase() === "unknown" && battery.batteryMv == null) ||
    (!battery.batteryText && battery.batteryMv === undefined)
  ) {
    return "";
  }
  const voltage = battery.batteryText || (typeof battery.batteryMv === "number" ? `${(battery.batteryMv / 1000).toFixed(3)} V` : "N/A");
  const state = battery.valid === false ? `invalid${battery.reason ? `: ${battery.reason}` : ""}` : battery.fresh === false ? "stale" : "fresh";
  return `Battery ${voltage} (${state})`;
}
