import { useEffect, useMemo, useState } from "react";

import type { BackendHttpClient } from "../../api/httpClient";
import type { BackendSnapshotPayload, MeasurementTransitionState, RowMeasurementMode, SetupProfile } from "../../api/types";
import { appliedMeasurementMode, isTransitionPending } from "../../state/measurement";

type Props = {
  client: BackendHttpClient | null;
  snapshot: BackendSnapshotPayload | null;
  setupProfile: SetupProfile;
  onSetupProfileChange: (profile: SetupProfile) => void;
  onError: (message: string) => void;
};

const rowModes: RowMeasurementMode[] = ["CAP", "VOLT", "RES"];

export function RowModeProfileControl({ client, snapshot, setupProfile, onSetupProfileChange, onError }: Props): JSX.Element {
  const [draftModes, setDraftModes] = useState<RowMeasurementMode[]>(() => [...setupProfile.acquisition.rowModes]);
  const [submitting, setSubmitting] = useState(false);
  const [localError, setLocalError] = useState("");

  useEffect(() => {
    setDraftModes([...setupProfile.acquisition.rowModes]);
  }, [setupProfile.acquisition.rowModes]);

  const view = useMemo(() => rowModeProfileView(snapshot), [snapshot]);
  const busy = submitting || view.busy;

  useEffect(() => {
    if (!submitting) {
      return;
    }
    if (
      view.pendingModes !== null ||
      view.transitionState === "error" ||
      view.transitionState === "timeout" ||
      profilesEqual(view.appliedModes, draftModes)
    ) {
      setSubmitting(false);
    }
  }, [draftModes, submitting, view]);

  function updateDraft(rowIndex: number, mode: RowMeasurementMode): void {
    setDraftModes((current) => current.map((value, index) => (index === rowIndex ? mode : value)));
    setLocalError("");
  }

  async function applyRowModes(): Promise<void> {
    if (!client || busy || draftModes.length !== 8) {
      return;
    }
    const modes = [...draftModes];
    setSubmitting(true);
    setLocalError("");
    onSetupProfileChange({
      ...setupProfile,
      acquisition: { ...setupProfile.acquisition, rowModes: modes }
    });
    try {
      // One request maps to one ROWMODES=<8 chars> firmware transaction.
      const response = await client.setRowModes({ modes });
      if (response.ok === false) {
        throw new Error(response.error || "Firmware rejected the row-mode profile");
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setSubmitting(false);
      setLocalError(message);
      onError(message);
    }
  }

  const activeRows = clampActiveRows(snapshot?.frame.rows ?? setupProfile.acquisition.rows);
  const changed = !profilesEqual(draftModes, view.appliedModes);
  const error = localError || view.error;

  return (
    <div className="controlGroup rowModeProfileControl" data-testid="row-mode-profile-control">
      <div className="panelHeader small">Row measurement modes</div>
      <div className="rowModeGrid">
        {draftModes.map((mode, rowIndex) => {
          const active = rowIndex < activeRows;
          return (
            <label key={rowIndex} className={active ? "rowModeEntry" : "rowModeEntry inactive"}>
              <span>S{rowIndex + 1}</span>
              <select
                aria-label={`S${rowIndex + 1} measurement mode`}
                value={mode}
                disabled={busy}
                onChange={(event) => updateDraft(rowIndex, event.target.value as RowMeasurementMode)}
              >
                {rowModes.map((candidate) => <option key={candidate} value={candidate}>{candidate}</option>)}
              </select>
              {!active ? <small>Inactive with current ROWS setting</small> : null}
            </label>
          );
        })}
      </div>
      <button className="primary" disabled={!client || busy || !changed} onClick={() => void applyRowModes()}>
        {busy ? "Applying row modes\u2026" : "Apply row modes"}
      </button>
      <dl className="rowModeStatus" data-testid="row-mode-status">
        <div><dt>Applied profile</dt><dd>{profileCode(view.appliedModes)}</dd></div>
        <div><dt>Pending profile</dt><dd>{view.pendingModes ? profileCode(view.pendingModes) : "\u2014"}</dd></div>
        <div><dt>State</dt><dd>{submitting ? "requested" : view.transitionState}</dd></div>
        {view.requestId !== null ? <div><dt>Request ID</dt><dd>{view.requestId}</dd></div> : null}
      </dl>
      {error ? <div className="inlineError compactMessage">{error}</div> : null}
    </div>
  );
}

export type RowModeProfileView = {
  appliedModes: RowMeasurementMode[];
  pendingModes: RowMeasurementMode[] | null;
  transitionState: MeasurementTransitionState;
  requestId: number | null;
  error: string;
  busy: boolean;
};

export function rowModeProfileView(snapshot: BackendSnapshotPayload | null): RowModeProfileView {
  const transaction = snapshot?.measurement?.rowProfile;
  const fallbackMode = appliedMeasurementMode(snapshot);
  const appliedModes = validProfile(transaction?.appliedModes)
    ?? validProfile(snapshot?.frame.rowModes)
    ?? validProfile(snapshot?.matrix.modeByRow)
    ?? Array.from({ length: 8 }, () => fallbackMode);
  return {
    appliedModes,
    pendingModes: validProfile(transaction?.pendingModes),
    transitionState: transaction?.transitionState ?? "applied",
    requestId: transaction?.requestId ?? null,
    error: transaction?.transitionState === "error" || transaction?.transitionState === "timeout" ? transaction.error : "",
    busy:
      isTransitionPending(transaction?.transitionState) ||
      isTransitionPending(snapshot?.measurement?.transitionState)
  };
}

export function profileCode(modes: RowMeasurementMode[]): string {
  return modes.map((mode) => mode === "CAP" ? "C" : mode === "VOLT" ? "V" : "R").join("");
}

function validProfile(value: RowMeasurementMode[] | null | undefined): RowMeasurementMode[] | null {
  if (!Array.isArray(value) || value.length !== 8 || value.some((mode) => !rowModes.includes(mode))) {
    return null;
  }
  return [...value];
}

function profilesEqual(left: RowMeasurementMode[], right: RowMeasurementMode[]): boolean {
  return left.length === right.length && left.every((mode, index) => mode === right[index]);
}

function clampActiveRows(value: number): number {
  return Math.max(1, Math.min(8, Math.trunc(Number.isFinite(value) ? value : 8)));
}
