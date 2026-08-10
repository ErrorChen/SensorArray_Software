import type { BackendHttpClient } from "../../api/httpClient";
import type { BackendSnapshotPayload, SetupProfile } from "../../api/types";
import { isCapacitanceMode } from "../../state/measurement";
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
  const setDefaultSaveDirectory = (defaultSaveDirectory: string) => {
    onSetupProfileChange({
      ...setupProfile,
      paths: { ...setupProfile.paths, defaultSaveDirectory }
    });
  };

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
