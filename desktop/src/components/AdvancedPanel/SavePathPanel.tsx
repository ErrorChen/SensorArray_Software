import { FolderOpen, RotateCcw, SearchCheck } from "lucide-react";
import { useState } from "react";

type Props = {
  directory: string;
  runtimeDirectory: string;
  onDirectoryChange: (directory: string) => void;
  onError: (message: string) => void;
  onNotice: (message: string) => void;
};

export function SavePathPanel({ directory, runtimeDirectory, onDirectoryChange, onError, onNotice }: Props): JSX.Element {
  const [pending, setPending] = useState(false);

  async function check(directoryToCheck: string): Promise<void> {
    setPending(true);
    try {
      const result = await window.sensorarrayDesktop?.setDefaultSaveDirectory(directoryToCheck);
      if (!result) {
        onError("Desktop save-directory API is unavailable");
        return;
      }
      onDirectoryChange(result.path);
      if (result.ok) {
        onNotice(`Default save directory: ${result.path}`);
      } else {
        onError(result.error || "Default save directory is not writable");
      }
    } finally {
      setPending(false);
    }
  }

  async function browse(): Promise<void> {
    setPending(true);
    try {
      const result = await window.sensorarrayDesktop?.selectDefaultSaveDirectory();
      if (!result || result.canceled) {
        return;
      }
      onDirectoryChange(result.path);
      if (result.ok) {
        onNotice(`Default save directory: ${result.path}`);
      } else {
        onError(result.error || "Default save directory is not writable");
      }
    } finally {
      setPending(false);
    }
  }

  async function openFolder(): Promise<void> {
    const result = await window.sensorarrayDesktop?.openDefaultSaveDirectory();
    if (!result?.ok) {
      onError(result?.error || "Could not open default save directory");
    }
  }

  return (
    <div className="savePathPanel">
      <label>
        Default save directory
        <input value={directory} onChange={(event) => onDirectoryChange(event.target.value)} onBlur={() => void check(directory)} />
      </label>
      <div className="buttonRow">
        <button disabled={pending} onClick={() => void browse()}>
          <FolderOpen size={16} /> Browse
        </button>
        <button disabled={pending} onClick={() => void check(directory)}>
          <SearchCheck size={16} /> Check
        </button>
        <button disabled={pending} onClick={() => void check(runtimeDirectory)}>
          <RotateCcw size={16} /> Reset to runtime directory
        </button>
        <button disabled={pending} onClick={() => void openFolder()}>
          <FolderOpen size={16} /> Open folder
        </button>
      </div>
    </div>
  );
}
