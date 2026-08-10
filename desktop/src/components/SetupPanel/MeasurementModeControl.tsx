import { useEffect, useMemo, useState } from "react";

import type { BackendHttpClient } from "../../api/httpClient";
import type { BackendSnapshotPayload, MeasurementMode, SetupProfile } from "../../api/types";
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

export type VoltageRailInputResult =
  | { ok: true; measuredAvddV?: number; measuredAvssV?: number }
  | { ok: false; error: string };

const modes: MeasurementMode[] = ["CAP", "VOLT", "RES"];

export function MeasurementModeControl({ client, snapshot, setupProfile, onSetupProfileChange, onError }: Props): JSX.Element {
  const [avddText, setAvddText] = useState(formatRailInput(setupProfile.voltageRail.measuredAvddV));
  const [avssText, setAvssText] = useState(formatRailInput(setupProfile.voltageRail.measuredAvssV));
  const [requestingMode, setRequestingMode] = useState<MeasurementMode | null>(null);
  const [localError, setLocalError] = useState("");

  useEffect(() => {
    setAvddText(formatRailInput(setupProfile.voltageRail.measuredAvddV));
    setAvssText(formatRailInput(setupProfile.voltageRail.measuredAvssV));
  }, [setupProfile.voltageRail.measuredAvddV, setupProfile.voltageRail.measuredAvssV]);

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
  const rail = snapshot?.measurement?.rail;
  const showVoltageRail = view.appliedMode !== "RES" || view.pendingMode === "VOLT" || requestingMode === "VOLT";

  async function requestMode(mode: MeasurementMode): Promise<void> {
    if (!client || view.busy || requestingMode) {
      return;
    }
    setLocalError("");
    const railInput = validateVoltageRailInputs(avddText, avssText, Boolean(rail?.configured));
    if (mode === "VOLT" && !railInput.ok) {
      setLocalError(railInput.error);
      onError(railInput.error);
      return;
    }

    const nextProfile: SetupProfile = {
      ...setupProfile,
      acquisition: { ...setupProfile.acquisition, measurementMode: mode },
      voltageRail:
        mode === "VOLT" && railInput.ok && railInput.measuredAvddV !== undefined && railInput.measuredAvssV !== undefined
          ? { measuredAvddV: railInput.measuredAvddV, measuredAvssV: railInput.measuredAvssV }
          : setupProfile.voltageRail
    };
    onSetupProfileChange(nextProfile);
    setRequestingMode(mode);
    try {
      const railFields =
        mode === "VOLT" && railInput.ok && railInput.measuredAvddV !== undefined && railInput.measuredAvssV !== undefined
          ? { measuredAvddV: railInput.measuredAvddV, measuredAvssV: railInput.measuredAvssV }
          : {};
      const response = await client.setMeasurementMode({ mode, ...railFields });
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

  function persistRailInputs(): void {
    const result = validateVoltageRailInputs(avddText, avssText, false);
    if (!result.ok || result.measuredAvddV === undefined || result.measuredAvssV === undefined) {
      return;
    }
    onSetupProfileChange({
      ...setupProfile,
      voltageRail: { measuredAvddV: result.measuredAvddV, measuredAvssV: result.measuredAvssV }
    });
  }

  return (
    <div className="controlGroup measurementModeControl" data-testid="measurement-mode-control">
      <div className="panelHeader small">Measurement Mode</div>
      <div className="measurementApplied">
        <span>Applied mode</span>
        <strong data-testid="measurement-applied-mode">{view.appliedMode}</strong>
        {view.pendingMode ? (
          <span className="measurementPending" data-testid="measurement-pending-mode">
            → {view.pendingMode}
          </span>
        ) : null}
      </div>
      <div className="measurementModeButtons" role="group" aria-label="Measurement mode">
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
        {requestingMode ? `Requesting ${requestingMode}…` : view.status}
      </div>

      {showVoltageRail ? (
        <fieldset className="voltageRailFields">
          <legend>Voltage measurement rails</legend>
          <p>Enter externally measured rail voltages. Nominal values are never assumed.</p>
          <label>
            AVDD to GND (V)
            <input
              aria-label="Measured AVDD to GND"
              inputMode="decimal"
              placeholder="e.g. 3.391"
              value={avddText}
              onChange={(event) => setAvddText(event.target.value)}
              onBlur={persistRailInputs}
            />
          </label>
          <label>
            AVSS to GND (V)
            <input
              aria-label="Measured AVSS to GND"
              inputMode="decimal"
              placeholder="e.g. -2.500"
              value={avssText}
              onChange={(event) => setAvssText(event.target.value)}
              onBlur={persistRailInputs}
            />
          </label>
          <div className="railState">
            Firmware rail: {rail?.configured ? "configured" : "not configured"}
            {rail?.state ? ` (${rail.state})` : ""}
          </div>
        </fieldset>
      ) : null}
      {localError || view.error ? <div className="inlineError compactMessage">{localError || view.error}</div> : null}
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
    busy: requestingMode !== null || isTransitionPending(measurement?.transitionState),
    error: measurement?.transitionState === "error" || measurement?.transitionState === "timeout" ? measurement.error || transitionDescription(snapshot) : ""
  };
}

export function validateVoltageRailInputs(avddText: string, avssText: string, railAlreadyConfigured: boolean): VoltageRailInputResult {
  const avddTrimmed = avddText.trim();
  const avssTrimmed = avssText.trim();
  if (!avddTrimmed && !avssTrimmed && railAlreadyConfigured) {
    return { ok: true };
  }
  const measuredAvddV = Number(avddTrimmed);
  const measuredAvssV = Number(avssTrimmed);
  if (!avddTrimmed || !avssTrimmed || !Number.isFinite(measuredAvddV) || !Number.isFinite(measuredAvssV)) {
    return { ok: false, error: "Voltage mode requires measured AVDD/AVSS rail configuration." };
  }
  if (measuredAvddV <= 0) {
    return { ok: false, error: "Measured AVDD must be greater than 0 V." };
  }
  if (measuredAvssV >= 0) {
    return { ok: false, error: "Measured AVSS must be less than 0 V." };
  }
  const railSpanV = measuredAvddV - measuredAvssV;
  if (railSpanV < 3.5 || railSpanV > 6.0) {
    return { ok: false, error: "Measured AVDD-AVSS span must be between 3.5 V and 6.0 V." };
  }
  return { ok: true, measuredAvddV, measuredAvssV };
}

function formatRailInput(value: number | null): string {
  return typeof value === "number" && Number.isFinite(value) ? String(value) : "";
}
