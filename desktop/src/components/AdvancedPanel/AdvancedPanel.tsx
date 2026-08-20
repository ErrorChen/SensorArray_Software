import { useState } from "react";

import type { BackendHttpClient } from "../../api/httpClient";
import type { BackendSnapshotPayload, SetupProfile } from "../../api/types";
import { isCapacitanceMode } from "../../state/measurement";
import { frontendPerformanceSnapshot } from "../../state/performanceInstrumentation";
import { OffsetPanel } from "./OffsetPanel";
import { SavePathPanel } from "./SavePathPanel";

type Props = {
  client: BackendHttpClient | null;
  snapshot: BackendSnapshotPayload | null;
  setupProfile: SetupProfile;
  runtimeDirectory: string;
  onSetupProfileChange: (profile: SetupProfile) => void;
  onError: (message: string) => void;
  onNotice: (message: string) => void;
};

export function AdvancedPanel({ client, snapshot, setupProfile, runtimeDirectory, onSetupProfileChange, onError, onNotice }: Props): JSX.Element {
  const [busy, setBusy] = useState<string | null>(null);
  const setDefaultSaveDirectory = (defaultSaveDirectory: string) => {
    onSetupProfileChange({
      ...setupProfile,
      paths: { ...setupProfile.paths, defaultSaveDirectory }
    });
  };

  async function run(label: string, action: (activeClient: BackendHttpClient) => Promise<unknown>): Promise<void> {
    if (!client || busy) return;
    setBusy(label);
    try {
      await action(client);
      onNotice(`${label} requested; waiting for authoritative device response.`);
    } catch (error) {
      onError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(null);
    }
  }

  const recording = snapshot?.recording;
  const calibration = snapshot?.calibration;
  const frontendPerformance = frontendPerformanceSnapshot();
  const backendWebSocket = snapshot?.performance?.webSocket as { activeSubscribers?: number } | undefined;
  const diagnostics = snapshot?.diagnostics ?? {};
  const recordingActive = recording?.state === "RECORDING" || recording?.state === "FINALIZING";
  const serial = snapshot?.connection.mode === "serial";
  const appliedProfile = snapshot?.measurement?.rowProfile?.appliedModes ?? [];
  const fdcEligible = appliedProfile.length === 8
    && appliedProfile.every((mode) => mode === appliedProfile[0])
    && (appliedProfile[0] === "VOLT" || appliedProfile[0] === "RES")
    && snapshot?.measurement?.appliedMode === appliedProfile[0]
    && snapshot?.measurement?.authoritativeStateKnown === true
    && snapshot?.measurement?.pendingMode == null
    && snapshot?.measurement?.rowProfile?.pendingModes == null
    && !snapshot?.fdcIsolation?.restartRequired;

  async function startScientificRecording(activeClient: BackendHttpClient): Promise<unknown> {
    if (serial && snapshot?.usbStream?.mode === "DEBUG") {
      if (window.confirm("USB stream is DEBUG and does not deliver every physical frame. Choose OK to switch to FULL before recording; choose Cancel for reduced-stream options.")) {
        await activeClient.setUsbStream("FULL");
        return activeClient.startRecording(setupProfile.paths.defaultSaveDirectory);
      }
      if (!window.confirm("Record the reduced DEBUG stream anyway? Missing physical frames will remain explicit in the recording.")) {
        return undefined;
      }
      return activeClient.startRecording(setupProfile.paths.defaultSaveDirectory, true);
    }
    return activeClient.startRecording(setupProfile.paths.defaultSaveDirectory);
  }

  return (
    <section className="advancedPanel">
      <div className="panelHeader">Advanced</div>
      <div className="controlGroup advancedGroup">
        <div className="panelHeader small">Default Save Directory</div>
        <SavePathPanel
          directory={setupProfile.paths.defaultSaveDirectory}
          runtimeDirectory={runtimeDirectory}
          onDirectoryChange={setDefaultSaveDirectory}
          onError={onError}
          onNotice={onNotice}
        />
      </div>
      <div className="controlGroup advancedGroup">
        <div className="panelHeader small">Scientific Recording</div>
        <div className="scanState">
          {recording?.state ?? "NOT_RECORDING"} · received {recording?.receivedFrames ?? 0} · written {recording?.writtenFrames ?? 0}
          {` · queue ${recording?.queueDepth ?? 0} · dropped ${recording?.droppedFrames ?? 0}`}
        </div>
        {recording?.error ? <div className="inlineError compactMessage">{recording.error}</div> : null}
        <button
          disabled={!client || busy !== null || recording?.state === "FINALIZING"}
          onClick={() => void run(recordingActive ? "Stop recording" : "Start recording", (activeClient) =>
            recordingActive
              ? activeClient.stopRecording()
              : startScientificRecording(activeClient)
          )}
        >
          {recordingActive ? "Stop & finalise" : "Start lossless recording"}
        </button>
      </div>
      <div className="controlGroup advancedGroup">
        <div className="panelHeader small">Device Lifecycle</div>
        <div className="scanState">
          boot {snapshot?.device?.bootId ?? "?"} · stage {snapshot?.device?.stage ?? "unknown"} · {snapshot?.device?.resetLabel ?? `reset ${snapshot?.device?.resetReason ?? "unknown"}`}
        </div>
        <div className="scanState">
          READY {String(snapshot?.device?.ready ?? "unknown")}
          {` · protocol ${deviceField(snapshot?.device?.protocol, "version")} (${deviceField(snapshot?.device?.protocol, "wires")})`}
          {` · compatible ${deviceField(snapshot?.device?.protocol, "compatible")}`}
        </div>
        <div className="scanState">
          build {deviceField(snapshot?.device?.build, "project")}
          {` · IDF ${deviceField(snapshot?.device?.build, "idf")}`}
          {` · target ${deviceField(snapshot?.device?.build, "target")}`}
          {` · proto ${deviceField(snapshot?.device?.build, "proto")}`}
        </div>
        {snapshot?.device?.powerRelated ? <div className="inlineError compactMessage">Power-related device reset detected. Check supply stability and cabling.</div> : null}
        <div className="inputRow">
          <button disabled={!client || busy !== null} onClick={() => void run("Recover", (activeClient) => activeClient.recoverDevice())}>Recover</button>
          <button
            disabled={!client || busy !== null}
            onClick={() => {
              if (window.confirm("Restart the connected SensorArray device? Recording will continue with a DEVICE_REBOOT boundary.")) {
                void run("Restart", (activeClient) => activeClient.restartDevice());
              }
            }}
          >Restart</button>
        </div>
      </div>
      <div className="controlGroup advancedGroup">
        <div className="panelHeader small">Presentation Diagnostics</div>
        <div className="scanState">
          charts {frontendPerformance.activeCharts} · resize observers {frontendPerformance.activeResizeObservers}
          {` · WebSocket client/server ${frontendPerformance.activeWebSockets}/${backendWebSocket?.activeSubscribers ?? "?"}`}
        </div>
        <div className="scanState">
          presented {frontendPerformance.presentedFrames} · {frontendPerformance.presentationRateHz.toFixed(1)} fps
          {` · coalesced ${frontendPerformance.coalescedSnapshots} · hidden ${frontendPerformance.hiddenSnapshots}`}
        </div>
        <div className="scanState">
          render {frontendPerformance.latestRenderMs?.toFixed(1) ?? "?"} ms · max {frontendPerformance.maximumRenderMs?.toFixed(1) ?? "?"} ms
          {` · long-frame warnings ${frontendPerformance.longFrameWarnings} · history points ${frontendPerformance.historyPoints}`}
        </div>
        {frontendPerformance.rendererHeapBytes !== null ? (
          <div className="scanState">renderer heap {(frontendPerformance.rendererHeapBytes / (1024 * 1024)).toFixed(1)} MiB</div>
        ) : null}
      </div>
      <div className="controlGroup advancedGroup">
        <div className="panelHeader small">Ingest / Data Integrity</div>
        <div className="scanState">
          expected output decimation {counter(diagnostics.expectedOutputDecimation)}
          {` · firmware non-fresh suppression ${counter(diagnostics.firmwareSuppressedNonFresh)}`}
          {` · firmware reported drops ${counter(diagnostics.firmwareReportedDrop)}`}
          {` · firmware-attributed seq gaps ${counter(diagnostics.firmwareAttributedSequenceGap)}`}
        </div>
        <div className="scanState">
          host ingress drops {counter(diagnostics.hostTransportDrop)}
          {` · parser rejects ${counter(diagnostics.parserRejects)}`}
          {` · awaiting firmware evidence ${counter(diagnostics.pendingFirmwareEvidenceGap)}`}
          {` · unexplained sequence gaps ${counter(diagnostics.hostUnexplainedSequenceGap)}`}
          {` · CRC errors ${counter(diagnostics.crcFailures)}`}
        </div>
        <div className="scanState">
          wire interleave recoveries {counter(diagnostics.wireInterleaveRecoveries)}
          {` · pending frames discarded by recovery ${counter(diagnostics.wireInterleaveDroppedFrames)}`}
        </div>
        <div className="scanState">
          parsed frames {counter(diagnostics.parserFrames)}
          {` · recorder received/written ${recording?.receivedFrames ?? 0}/${recording?.writtenFrames ?? 0}`}
          {` · UI coalesced ${frontendPerformance.coalescedSnapshots}`}
        </div>
      </div>
      <div className="controlGroup advancedGroup">
        <div className="panelHeader small">USB Stream</div>
        <div className="segmented">
          {(["DEBUG", "FULL"] as const).map((mode) => (
            <button
              key={mode}
              className={snapshot?.usbStream?.mode === mode ? "active" : ""}
              disabled={!client || !serial || busy !== null}
              onClick={() => void run(`USB stream ${mode}`, (activeClient) => activeClient.setUsbStream(mode))}
            >{mode}</button>
          ))}
        </div>
        {!serial ? <div className="modeOnlyNotice">USB stream policy applies only to the Serial USB sink.</div> : null}
      </div>
      <div className="controlGroup advancedGroup">
        <div className="panelHeader small">FDC Isolation</div>
        <div className="scanState">
          SD {String(snapshot?.fdcIsolation?.sd ?? "unknown")} · verified {String(snapshot?.fdcIsolation?.verified ?? "unknown")}
          {snapshot?.fdcIsolation?.restartRequired ? " · restart required" : ""}
        </div>
        <div className="inputRow">
          <button disabled={!client || !fdcEligible || busy !== null} onClick={() => void run("Enable FDC isolation", (activeClient) => activeClient.setFdcIsolation(true))}>Enable</button>
          <button disabled={!client || Boolean(snapshot?.fdcIsolation?.restartRequired) || busy !== null} onClick={() => void run("Disable FDC isolation", (activeClient) => activeClient.setFdcIsolation(false))}>Disable</button>
        </div>
        {snapshot?.fdcIsolation?.restartRequired ? (
          <div className="modeOnlyNotice">FDC shutdown is active. CAP is unavailable until device restart. Restart is required to reinitialise FDC frontends.</div>
        ) : !fdcEligible ? (
          <div className="modeOnlyNotice">FDC isolation can be enabled only after bootstrap confirms a homogeneous VOLT or RES profile.</div>
        ) : null}
        {snapshot?.fdcIsolation?.verified ? <div className="scanState">FDC SD command/readback verified.</div> : null}
      </div>
      <div className="controlGroup advancedGroup">
        <div className="panelHeader small">Calibration</div>
        <div className="scanState">
          {!calibration
            ? "Calibration state unavailable"
            : calibration.valid
              ? "Calibrated"
              : "Uncalibrated/default"}
          {calibration
            ? ` · state ${calibration.state || "snapshot"} · reason ${calibration.rawFields?.reason || "none"} · source ${calibration.source || "unknown"} · schema ${calibration.schema ?? "unknown"} · board ${calibration.boardId || "unknown"} · hardware rev ${calibration.hardwareRev ?? "unknown"} · payload ${calibration.payloadLength ?? "unknown"} bytes`
            : ""}
        </div>
        <div className="inputRow">
          <button disabled={!client || busy !== null} onClick={() => void run("Load calibration", (activeClient) => activeClient.loadCalibration())}>Load</button>
          <button
            disabled={!client || busy !== null}
            onClick={() => {
              if (window.confirm("Save current matrix calibration to device persistent calibration storage? This writes flash.")) {
                void run("Save calibration", (activeClient) => activeClient.saveCalibration());
              }
            }}
          >Save to device</button>
        </div>
      </div>
      <div className="controlGroup advancedGroup">
        <div className="panelHeader small">Offset</div>
        {isCapacitanceMode(snapshot) ? (
          <OffsetPanel client={client} snapshot={snapshot} onError={onError} />
        ) : (
          <div className="modeOnlyNotice">User offset calibration is available in capacitance mode only. Voltage and resistance values are not modified.</div>
        )}
      </div>
    </section>
  );
}

function counter(value: unknown): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : 0;
}

function deviceField(value: Record<string, unknown> | null | undefined, key: string): string {
  const field = value?.[key];
  return field === null || field === undefined || field === "" ? "unknown" : String(field);
}
