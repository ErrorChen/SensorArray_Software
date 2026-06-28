import type { SetupProfile } from "../api/types";
import { defaultSetupProfile, normaliseSetupProfile } from "./setupProfile";

const setupProfileStorageKey = "sensorarray.setupProfile";

export function readStoredSetupProfile(runtimeDirectory: string): SetupProfile {
  const raw = window.localStorage.getItem(setupProfileStorageKey);
  if (!raw) {
    return defaultSetupProfile(runtimeDirectory);
  }
  try {
    return normaliseSetupProfile(JSON.parse(raw), runtimeDirectory);
  } catch {
    return defaultSetupProfile(runtimeDirectory);
  }
}

export function writeStoredSetupProfile(profile: SetupProfile): void {
  window.localStorage.setItem(setupProfileStorageKey, JSON.stringify(profile));
}
