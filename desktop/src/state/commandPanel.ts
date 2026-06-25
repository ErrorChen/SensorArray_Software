export function isCommandSendDisabled(options: {
  hasClient: boolean;
  pending: boolean;
  busy: boolean;
  connected: boolean;
  commandText: string;
}): boolean {
  return !options.hasClient || options.pending || options.busy || !options.connected || options.commandText.trim().length === 0;
}

export function updateCommandHistory(current: string[], commandText: string, limit = 20): string[] {
  const trimmed = commandText.trim();
  if (!trimmed) {
    return current.slice(0, limit);
  }
  return [trimmed, ...current.filter((item) => item !== trimmed)].slice(0, limit);
}
