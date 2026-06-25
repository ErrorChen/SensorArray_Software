import type { TransportMode } from "../api/types";

const activeStates = new Set(["connecting", "connected", "streaming", "reconnecting"]);

export function isBleScanDisabled(connectionMode: TransportMode | string | undefined, connectionState: string | undefined): boolean {
  return connectionMode === "ble" && activeStates.has((connectionState ?? "disconnected").toLowerCase());
}
