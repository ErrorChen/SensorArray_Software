import { useEffect, useMemo, useState } from "react";

import type { BackendHttpClient } from "../../api/httpClient";
import type { BackendSnapshotPayload, MeasurementMode, RailTelemetry, SetupProfile } from "../../api/types";
import { appliedMeasurementMode, isTransitionPending, transitionDescription } from "../../state/measurement";

type Props = {
  client: BackendHttpClient | null;
  snapshot: BackendSnapshotPayload | null;
  setupProfile: SetupProfile;
  onSetupProfileChange: (profile: SetupProfile) => void;
  onError: (message: string) => void;
};

export type MeasurementControlView = {
  appliedMode: MeasurementMode;
  pendingMode: MeasurementMode | null;
  transitionState: string;
  status: string;
  requestId: number | null;
  busy: boolean;
  error: string;
};

const modes: MeasurementMode[] = ["CAP", "VOLT", "RES"];

export function MeasurementModeControl({ client, snapshot, setupProfile, onSetupProfileChange, onError }: Props): JSX.Element {
  const [requestingMode, setRequestingMode] = useState<MeasurementMode | null>(null);
  const [localError, setLocalError] = useState("");

  useEffect(() => {
    const measurement = snapshot?.measurement;
    if (
      requestingMode &&
      measurement &&
      (measurement.pendingMode === requestingMode ||
        measurement.appliedMode === requestingMode ||
        measurement.transitionState === "error" ||
        measurement.transitionState === "timeout")
    ) {
      setRequestingMode(null);
    }
  }, [requestingMode, snapshot?.measurement]);

  const view = useMemo(() => measurementControlView(snapshot, requestingMode), [requestingMode, snapshot]);

  async function requestMode(mode: MeasurementMode): Promise<void> {
    if (!client || view.busy || requestingMode) {
      return;
    }
    setLocalError("");
    onSetupProfileChange({
      ...setupProfile,
      acquisition: {
        ...setupProfile.acquisition,
        measurementMode: mode,
        rowModes: Array.from({ length: 8 }, () => mode)
      }
    });
    setRequestingMode(mode);
    try {
      // MODE remains the backward-compatible, atomic firmware quick action.
      // Rail acquisition is owned by firmware and is never a prerequisite.
      const response = await client.setMeasurementMode({ mode });
      if (response.ok === false) {
        throw new Error(response.error || `Firmware rejected ${mode} mode`);
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setLocalError(message);
      onError(message);
      setRequestingMode(null);
    }
  }

  return (
    <div className="controlGroup measurementModeControl" data-testid="measurement-mode-control">
      <div className="panelHeader small">Measurement Mode</div>
      <div className="measurementApplied">
        <span>Applied mode</span>
        <strong data-testid="measurement-applied-mode">{view.appliedMode}</strong>
        {view.pendingMode ? (
          <span className="measurementPending" data-testid="measurement-pending-mode">
            {"\u2192"} {view.pendingMode}
          </span>
        ) : null}
      </div>
      <div className="modeActionLabel">Set all rows:</div>
      <div className="measurementModeButtons" role="group" aria-label="Set all row measurement modes">
        {modes.map((mode) => (
          <button
            key={mode}
            className={view.appliedMode === mode && !view.pendingMode ? "active" : ""}
            disabled={!client || view.busy || requestingMode !== null}
            aria-pressed={view.appliedMode === mode && !view.pendingMode}
            onClick={() => void requestMode(mode)}
          >
            {mode}
          </button>
        ))}
      </div>
      <div className={`measurementTransition ${view.error || localError ? "error" : ""}`} data-testid="measurement-transition-state">
        {requestingMode ? `Requesting ${requestingMode}\u2026` : view.status}
      </div>
      <RailTelemetryReadout telemetry={snapshot?.measurement?.railTelemetry} />
      {localError || view.error ? <div className="inlineError compactMessage">{localError || view.error}</div> : null}
    </div>
  );
}

export function RailTelemetryReadout({ telemetry }: { telemetry: RailTelemetry | undefined }): JSX.Element {
  const railSpanUv = typeof telemetry?.railSpanUv === "number" && Number.isFinite(telemetry.railSpanUv)
    ? telemetry.railSpanUv
    : null;
  const span = railSpanUv !== null
    ? `${(railSpanUv / 1_000_000).toFixed(3)} V`
    : "Rail unavailable";
  const age = telemetryAgeSeconds(telemetry);
  const staleReason = String(telemetry?.reason || "").toLowerCase();
  const retainedStale = railSpanUv !== null && telemetry?.fresh === false && (
    telemetry.valid || ["stale", "hold", "connection_stale"].includes(staleReason)
  );
  const state = telemetry?.fresh
    ? "fresh"
    : retainedStale
      ? `stale${age === null ? "" : ` ${formatAge(age)}`}`
      : telemetry?.reason || "unavailable";
  return (
    <div className="railTelemetry" data-testid="rail-telemetry">
      <div className="railTelemetryTitle">ADS analogue rail span</div>
      <strong>AVDD {"\u2212"} AVSS: {span}</strong>
      {telemetry?.valid ? <span>{state}</span> : <span>{state}</span>}
      {telemetry?.valid && telemetry.fresh && age !== null ? <span>age: {formatAge(age)}</span> : null}
      {telemetry?.source ? <span>source: {telemetry.source.replace(/_/g, " ")}</span> : null}
    </div>
  );
}

export function measurementControlView(
  snapshot: BackendSnapshotPayload | null,
  requestingMode: MeasurementMode | null = null
): MeasurementControlView {
  const appliedMode = appliedMeasurementMode(snapshot);
  const measurement = snapshot?.measurement;
  const pendingMode = measurement?.pendingMode ?? requestingMode;
  const transitionState = requestingMode ? "requested" : measurement?.transitionState ?? "applied";
  return {
    appliedMode,
    pendingMode,
    transitionState,
    status: requestingMode ? `Requesting ${requestingMode}` : transitionDescription(snapshot),
    requestId: measurement?.requestId ?? null,
    busy:
      requestingMode !== null ||
      isTransitionPending(measurement?.transitionState) ||
      isTransitionPending(measurement?.rowProfile?.transitionState),
    error: measurement?.transitionState === "error" || measurement?.transitionState === "timeout" ? measurement.error || transitionDescription(snapshot) : ""
  };
}

function telemetryAgeSeconds(telemetry: RailTelemetry | undefined): number | null {
  const value = telemetry?.ageSeconds ?? telemetry?.age;
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

function formatAge(seconds: number): string {
  return seconds < 10 ? `${seconds.toFixed(1)} s` : `${Math.round(seconds)} s`;
}
