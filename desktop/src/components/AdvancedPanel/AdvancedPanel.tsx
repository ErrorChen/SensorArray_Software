import type { BackendHttpClient } from "../../api/httpClient";
import type { BackendSnapshotPayload } from "../../api/types";
import { OffsetPanel } from "./OffsetPanel";

type Props = {
  client: BackendHttpClient | null;
  snapshot: BackendSnapshotPayload | null;
  onError: (message: string) => void;
};

export function AdvancedPanel({ client, snapshot, onError }: Props): JSX.Element {
  return (
    <section className="advancedPanel">
      <div className="panelHeader">Advanced</div>
      <div className="controlGroup advancedGroup">
        <div className="panelHeader small">Offset</div>
        <OffsetPanel client={client} snapshot={snapshot} onError={onError} />
      </div>
    </section>
  );
}
