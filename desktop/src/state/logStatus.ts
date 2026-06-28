import type { LogRow } from "../api/types";
import { logTagSchemas, type LogFieldValue, type LogTagSchema, type StatusCategory, type StatusSeverity } from "./logSchema";

export type { StatusCategory, StatusSeverity } from "./logSchema";

export interface StatusItem {
  category: StatusCategory;
  severity: StatusSeverity;
  title: string;
  explanation: string;
  details: Record<string, LogFieldValue | string>;
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

export function parseKeyValueText(rawText: string): Record<string, LogFieldValue> {
  const parts = splitCsv(rawText);
  const details: Record<string, LogFieldValue> = {};
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
  const rowTag = (row.tag || "UNKNOWN").trim();
  const rawTag = (row.rawText.split(",", 1)[0] || "").trim();
  const tag = logTagSchemas[rowTag] ? rowTag : rawTag || rowTag;
  const details = { ...parseKeyValueText(row.rawText), ...row.parsedFields };
  const base = {
    lastSeen: row.timestamp ? new Date(row.timestamp * 1000).toLocaleTimeString() : undefined
  };
  const schema = logTagSchemas[tag];
  if (schema) {
    return buildStatusItemFromSchema(row, tag, schema, details);
  }
  if (tag === "PARSER" || containsAny(row.rawText, ["crc", "length", "reject", "malformed", "strict_ascii"])) {
    return {
      ...base,
      category: "Parser",
      severity: row.severity === "error" ? "error" : "warn",
      title: "Parser issue",
      explanation: "The parser rejected or warned about malformed input.",
      details: labelDetails(details, {}, "Legacy/unknown field")
    };
  }
  if (containsAny(row.rawText, ["baseline", "Baseline"])) {
    return {
      ...base,
      category: "Baseline",
      severity: row.severity === "error" || containsAny(row.rawText, ["invalid", "Invalid", "No data"]) ? "warn" : "info",
      title: "Baseline state",
      explanation: "Baseline capture, ready, reset, or invalidation state.",
      details: labelDetails(details, {}, "Legacy/unknown field")
    };
  }
  if (containsAny(row.rawText, ["connected", "disconnected", "reconnecting", "CONNECTING", "STREAMING"])) {
    return {
      ...base,
      category: transportCategory(row.source),
      severity: row.severity === "error" ? "error" : "info",
      title: "Transport state",
      explanation: "Connection lifecycle state reported by the backend transport manager.",
      details: labelDetails(details, {}, "Legacy/unknown field")
    };
  }
  return {
    ...base,
    category: "Other",
    severity: normaliseSeverity(row.severity),
    title: `Unknown firmware log (${tag || "UNKNOWN"})`,
    explanation: Object.keys(details).length ? "Unknown firmware log with parsed key/value fields." : "Unknown firmware log line.",
    details: labelDetails(details, {}, "Unknown firmware field")
  };
}

function buildStatusItemFromSchema(row: LogRow, tag: string, schema: LogTagSchema, details: Record<string, LogFieldValue>): StatusItem {
  const severity = typeof schema.severity === "function" ? schema.severity(row, details) : schema.severity ?? normaliseSeverity(row.severity);
  return {
    category: schema.category,
    severity,
    title: schema.title,
    explanation: schema.explanation,
    details: labelDetails(details, schema.fieldLabels, "Legacy/unknown field", schema.fieldFormatters),
    lastSeen: row.timestamp ? new Date(row.timestamp * 1000).toLocaleTimeString() : undefined
  };
}

function labelDetails(
  details: Record<string, LogFieldValue>,
  fieldLabels: Record<string, string>,
  unknownPrefix: string,
  fieldFormatters: Record<string, (value: LogFieldValue) => string> = {}
): Record<string, LogFieldValue | string> {
  const output: Record<string, LogFieldValue | string> = {};
  for (const [key, value] of Object.entries(details)) {
    const label = fieldLabels[key] ?? `${unknownPrefix} (${key})`;
    output[label] = fieldFormatters[key] ? fieldFormatters[key](value) : value;
  }
  return output;
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

function coerceValue(value: string): LogFieldValue {
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
