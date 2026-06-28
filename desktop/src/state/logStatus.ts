import type { LogRow } from "../api/types";

export type StatusSeverity = "ok" | "warn" | "error" | "info";

export type StatusCategory =
  | "Transport"
  | "BLE"
  | "Wi-Fi"
  | "Serial"
  | "Parser"
  | "Rows"
  | "Baseline"
  | "Display"
  | "Error"
  | "Other";

export interface StatusItem {
  category: StatusCategory;
  severity: StatusSeverity;
  title: string;
  explanation: string;
  details: Record<string, string | number | boolean | null>;
  lastSeen?: string;
}

export function parseLogStatusRows(rows: LogRow[]): StatusItem[] {
  const latest = new Map<string, StatusItem>();
  for (const row of rows) {
    const item = parseLogRow(row);
    const key = `${item.category}:${item.title}`;
    latest.set(key, item);
  }
  return Array.from(latest.values()).sort((left, right) => severityRank(right.severity) - severityRank(left.severity));
}

export function parseKeyValueText(rawText: string): Record<string, string | number | boolean | null> {
  const parts = splitCsv(rawText);
  const details: Record<string, string | number | boolean | null> = {};
  for (const part of parts.slice(1)) {
    const index = part.indexOf("=");
    if (index < 0) {
      continue;
    }
    details[part.slice(0, index).trim()] = coerceValue(part.slice(index + 1).trim());
  }
  return details;
}

function parseLogRow(row: LogRow): StatusItem {
  const tag = (row.tag || row.rawText.split(",", 1)[0] || "UNKNOWN").trim();
  const details = { ...parseKeyValueText(row.rawText), ...row.parsedFields };
  const base = {
    details,
    lastSeen: row.timestamp ? new Date(row.timestamp * 1000).toLocaleTimeString() : undefined
  };
  if (tag === "CMD_TX") {
    return {
      ...base,
      category: "Transport",
      severity: "ok",
      title: "Command sent",
      explanation: "A command was written through the active backend transport."
    };
  }
  if (tag === "CMD_TX_FAIL") {
    return {
      ...base,
      category: "Error",
      severity: "error",
      title: "Command send failed",
      explanation: "The active transport rejected or failed the write request."
    };
  }
  if (tag === "BLE_RX50") {
    return {
      ...base,
      category: "BLE",
      severity: severityFromCounters(row, details),
      title: "BLE notify receive statistics",
      explanation: "Periodic BLE packet, byte, failure, and prefix counters."
    };
  }
  if (tag === "BLE_FRAG50") {
    return {
      ...base,
      category: "BLE",
      severity: severityFromCounters(row, details),
      title: "BLE fragment statistics",
      explanation: "BLE fragment reassembly counters including duplicate, missing, timeout, CRC, and length failures."
    };
  }
  if (tag === "PROTO50") {
    return {
      ...base,
      category: "Parser",
      severity: severityFromCounters(row, details),
      title: "Protocol parser statistics",
      explanation: "Content router counters for capacitance frames, text logs, rejects, and accepted frames."
    };
  }
  if (tag === "RCMD") {
    return {
      ...base,
      category: "Rows",
      severity: "ok",
      title: "Rows command accepted",
      explanation: "Firmware accepted a ROWS request; application still waits for the frame-boundary apply event."
    };
  }
  if (tag === "RAPP") {
    return {
      ...base,
      category: "Rows",
      severity: "ok",
      title: "Rows applied",
      explanation: "Firmware applied the requested row count at a frame boundary."
    };
  }
  if (tag === "PARSER" || containsAny(row.rawText, ["crc", "length", "reject", "malformed", "strict_ascii"])) {
    return {
      ...base,
      category: "Parser",
      severity: row.severity === "error" ? "error" : "warn",
      title: "Parser issue",
      explanation: "The parser rejected or warned about malformed input."
    };
  }
  if (containsAny(row.rawText, ["baseline", "Baseline"])) {
    return {
      ...base,
      category: "Baseline",
      severity: row.severity === "error" || containsAny(row.rawText, ["invalid", "Invalid", "No data"]) ? "warn" : "info",
      title: "Baseline state",
      explanation: "Baseline capture, ready, reset, or invalidation state."
    };
  }
  if (containsAny(row.rawText, ["connected", "disconnected", "reconnecting", "CONNECTING", "STREAMING"])) {
    return {
      ...base,
      category: transportCategory(row.source),
      severity: row.severity === "error" ? "error" : "info",
      title: "Transport state",
      explanation: "Connection lifecycle state reported by the backend transport manager."
    };
  }
  return {
    ...base,
    category: "Other",
    severity: normaliseSeverity(row.severity),
    title: tag || "Unknown log",
    explanation: Object.keys(details).length ? "Unrecognised log with parsed key/value fields." : "Unrecognised log line.",
    details
  };
}

function severityFromCounters(row: LogRow, details: Record<string, string | number | boolean | null>): StatusSeverity {
  if (row.severity === "error") {
    return "error";
  }
  const badKeys = ["fail", "reject", "missing", "timeout", "crc", "length"];
  return badKeys.some((key) => Number(details[key] ?? 0) > 0) ? "warn" : "ok";
}

function normaliseSeverity(value: string): StatusSeverity {
  if (value === "error") {
    return "error";
  }
  if (value === "warning" || value === "warn") {
    return "warn";
  }
  if (value === "info" || value === "ok") {
    return value;
  }
  return "info";
}

function transportCategory(source: string): StatusCategory {
  if (source === "ble") {
    return "BLE";
  }
  if (source === "wifi") {
    return "Wi-Fi";
  }
  if (source === "serial") {
    return "Serial";
  }
  return "Transport";
}

function containsAny(value: string, needles: string[]): boolean {
  return needles.some((needle) => value.includes(needle));
}

function splitCsv(value: string): string[] {
  const out: string[] = [];
  let current = "";
  let quoted = false;
  for (const char of value) {
    if (char === '"') {
      quoted = !quoted;
      continue;
    }
    if (char === "," && !quoted) {
      out.push(current.trim());
      current = "";
    } else {
      current += char;
    }
  }
  out.push(current.trim());
  return out;
}

function coerceValue(value: string): string | number | boolean | null {
  if (value === "") {
    return null;
  }
  if (value === "true" || value === "false") {
    return value === "true";
  }
  const number = Number(value);
  return Number.isFinite(number) && value.trim() !== "" ? number : value;
}

function severityRank(value: StatusSeverity): number {
  return { error: 4, warn: 3, info: 2, ok: 1 }[value];
}
