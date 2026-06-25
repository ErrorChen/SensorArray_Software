import { Activity, CircleAlert, RadioTower } from "lucide-react";

import type { BackendSnapshotPayload } from "../../api/types";

type Props = {
  snapshot: BackendSnapshotPayload | null;
  socketState: string;
};

export function StatusBar({ snapshot, socketState }: Props): JSX.Element {
  const connection = snapshot?.connection;
  const frame = snapshot?.frame;
  const error = connection?.error;
  return (
    <header className="statusBar">
      <div className="brand">SensorArray</div>
      <div className="statusItems">
        <span className="statusItem">
          <RadioTower size={16} /> {connection?.mode ?? "serial"} / {connection?.state ?? "disconnected"}
        </span>
        <span className="statusItem">{connection?.deviceLabel || "No device"}</span>
        <span className="statusItem">
          <Activity size={16} /> seq {frame?.seq ?? "-"} / {frame?.fps?.toFixed(1) ?? "0.0"} fps
        </span>
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
