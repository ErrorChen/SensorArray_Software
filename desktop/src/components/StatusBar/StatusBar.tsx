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
        {(snapshot?.transport?.reconnectAttempt ?? 0) > 0 ? (
          <span className="statusWarning">
            reconnect {snapshot?.transport?.reconnectAttempt} · {(snapshot?.transport?.reconnectBackoff ?? 0).toFixed(1)}s
          </span>
        ) : null}
        <span className="statusItem">boot {snapshot?.device?.bootId ?? "-"}</span>
        <span className="statusItem">
          <Activity size={16} /> Measurement: {appliedMode}
          {pendingMode ? ` \u2192 ${pendingMode} (${snapshot?.measurement.transitionState ?? "requested"})` : ""}
        </span>
        <span className="statusItem">seq {frame?.seq ?? "-"}</span>
        {rateItems(snapshot).map((item) => (
          <span key={item.label} className="statusItem">
            {item.label} {item.value.toFixed(1)} fps
          </span>
        ))}
        {!snapshot?.rates && typeof frame?.fps === "number" ? <span className="statusItem">Host frames {frame.fps.toFixed(1)} fps</span> : null}
        <span className="statusItem">{battery}</span>
        {snapshot?.recording?.state === "RECORDING" ? (
          <span className={snapshot.recording.droppedFrames ? "statusWarning" : "statusItem"}>
            REC {snapshot.recording.writtenFrames}/{snapshot.recording.receivedFrames} · drop {snapshot.recording.droppedFrames}
          </span>
        ) : null}
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

export function batteryStatus(snapshot: BackendSnapshotPayload | null): string {
  const battery = snapshot?.battery;
  if (!battery) {
    return "Battery \u2014";
  }
  const latest = battery.latestAttempt;
  const lastGood = battery.lastGood;
  const latestMv = finiteNumber(latest?.batteryMv);
  const goodMv = finiteNumber(lastGood?.batteryMv) ?? (battery.valid !== false ? finiteNumber(battery.batteryMv) : null);
  const displayedMv = latest?.valid !== false && latestMv !== null ? latestMv : goodMv ?? finiteNumber(battery.batteryMv);
  const displayedText = latest?.valid !== false && latest?.batteryText
    ? latest.batteryText
    : lastGood?.batteryText || battery.batteryText || (displayedMv !== null ? `${(displayedMv / 1000).toFixed(3)} V` : "");
  if (!displayedText || displayedText.toUpperCase() === "N/A") {
    return "Battery \u2014";
  }
  // The top-level battery state is computed at snapshot time, so it carries
  // connection/session staleness that the retained latest attempt cannot
  // know about.  latestAttempt remains useful for a failed-attempt reason and
  // numeric fallback, but must never relabel a connection-stale value fresh.
  const valid = battery.valid ?? latest?.valid;
  const fresh = battery.fresh
    ?? (battery.state === "fresh" ? true : battery.state ? false : latest?.fresh);
  const snapshotReason = String(battery.reason ?? "").trim();
  const reason = snapshotReason && snapshotReason !== "ok"
    ? snapshotReason
    : String(latest?.reason ?? snapshotReason).trim();
  if (valid === false) {
    return `Battery ${displayedText} (last known \u00B7 ${reason || "measurement failed"})`;
  }
  if (fresh === false) {
    return `Battery ${displayedText} (last known \u00B7 ${reason || "stale"})`;
  }
  return `Battery ${displayedText} (fresh)`;
}

function finiteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}
