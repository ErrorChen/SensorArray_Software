import { FolderOpen, RotateCcw, SearchCheck } from "lucide-react";
import { useMemo, useState } from "react";

import type { DesktopBridge, PathCheckResult } from "../../api/types";

type Props = {
  directory: string;
  runtimeDirectory: string;
  onDirectoryChange: (directory: string) => void;
  onError: (message: string) => void;
  onNotice: (message: string) => void;
};

export function SavePathPanel({ directory, runtimeDirectory, onDirectoryChange, onError, onNotice }: Props): JSX.Element {
  const [pending, setPending] = useState(false);
  const apiStatus = useMemo(
    () => desktopApiStatus(["setDefaultSaveDirectory", "selectDefaultSaveDirectory", "openDefaultSaveDirectory"]),
    []
  );

  async function check(directoryToCheck: string): Promise<void> {
    const api = requireDesktopApi(apiStatus, onError);
    if (!api) {
      return;
    }
    setPending(true);
    try {
      const result = await api.setDefaultSaveDirectory(directoryToCheck);
      reportPathCheck(result);
    } finally {
      setPending(false);
    }
  }

  async function browse(): Promise<void> {
    const api = requireDesktopApi(apiStatus, onError);
    if (!api) {
      return;
    }
    setPending(true);
    try {
      const result = await api.selectDefaultSaveDirectory();
      if (result.canceled) {
        return;
      }
      reportPathCheck(result);
    } finally {
      setPending(false);
    }
  }

  async function openFolder(): Promise<void> {
    const api = requireDesktopApi(apiStatus, onError);
    if (!api) {
      return;
    }
    const checkResult = await api.setDefaultSaveDirectory(directory);
    if (!checkResult.ok) {
      reportPathCheck(checkResult);
      return;
    }
    const result = await api.openDefaultSaveDirectory();
    if (!result?.ok) {
      onError(result?.error || "Could not open default save directory");
    }
  }

  function reportPathCheck(result: PathCheckResult): void {
    onDirectoryChange(result.path);
    if (result.ok) {
      onNotice(`Default save directory: ${result.path}`);
    } else {
      onError(result.error || "Default save directory is not writable");
    }
  }

  return (
    <div className="savePathPanel">
      {!apiStatus.ok ? <div className="inlineError compactMessage">{apiStatus.message}</div> : null}
      <label>
        Default save directory
        <input value={directory} onChange={(event) => onDirectoryChange(event.target.value)} />
      </label>
      <div className="buttonRow">
        <button disabled={pending || !apiStatus.ok} onClick={() => void browse()}>
          <FolderOpen size={16} /> Browse
        </button>
        <button disabled={pending || !apiStatus.ok} onClick={() => void check(directory)}>
          <SearchCheck size={16} /> Check
        </button>
        <button disabled={pending || !apiStatus.ok} onClick={() => void check(runtimeDirectory)}>
          <RotateCcw size={16} /> Reset to runtime directory
        </button>
        <button disabled={pending || !apiStatus.ok} onClick={() => void openFolder()}>
          <FolderOpen size={16} /> Open folder
        </button>
      </div>
    </div>
  );
}

type DesktopApiStatus = { ok: true; api: DesktopBridge } | { ok: false; message: string };

function desktopApiStatus(methods: (keyof DesktopBridge)[]): DesktopApiStatus {
  const api = window.sensorarrayDesktop;
  if (!api) {
    return {
      ok: false,
      message: isElectronUserAgent() ? "Electron preload failure: window.sensorarrayDesktop is missing." : "Desktop file APIs are only available in Electron."
    };
  }
  const missing = methods.find((method) => typeof api[method] !== "function");
  if (missing) {
    return { ok: false, message: `Electron preload API is incomplete: missing ${String(missing)}.` };
  }
  return { ok: true, api };
}

function requireDesktopApi(status: DesktopApiStatus, onError: (message: string) => void): DesktopBridge | null {
  if (!status.ok) {
    onError(status.message);
    return null;
  }
  return status.api;
}

function isElectronUserAgent(): boolean {
  return navigator.userAgent.toLowerCase().includes(" electron/");
}
