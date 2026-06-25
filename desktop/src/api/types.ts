export type TransportMode = "serial" | "ble" | "wifi" | "replay";
export type DisplayMode = "absolute_pf" | "delta_percent";

export type ConnectionSnapshot = {
  mode: TransportMode;
  state: string;
  deviceLabel: string;
  generation: number;
  error?: string;
};

export type FrameSnapshot = {
  seq: number | null;
  fps: number;
  rows: number;
  valid: boolean;
  timestampUs: number | null;
  revision: number;
};

export type MatrixSnapshot = {
  rows: string[];
  cols: string[];
  correctedPf: (number | null)[][];
  rawPf: (number | null)[][];
  rawFixed: (number | null)[][];
  displayValues: (number | null)[][];
  validMask: boolean[][];
  unit: "pF" | "%";
  domain: string;
};

export type SelectionSnapshot = {
  rowIndex: number;
  rowLabel: string;
  fdcGroup: "primary" | "secondary";
  detectorStart: number;
  detectorEnd: number;
  cells: string[];
  title: string;
  selectionRevision: number;
};

export type DisplaySnapshot = {
  displayMode: DisplayMode;
  measurementDomain: string;
  showCellText: boolean;
  pauseDisplay: boolean;
  freezeColor: boolean;
  unitMode: string;
  circuitOffsetPf: number;
  colorRange: {
    min: number | null;
    max: number | null;
    frozen: boolean;
  };
};

export type LogRow = {
  timestamp: number;
  monotonicTime: number;
  source: string;
  channel: string;
  tag: string;
  severity: string;
  rawText: string;
  parsedFields: Record<string, string>;
  recognised: boolean;
  sessionGeneration: number;
};

export type LogsSnapshot = {
  revision: number;
  totalRecords: number;
  overwrites: number;
  rows: LogRow[];
};

export type BleDevice = {
  name: string;
  address: string;
  rssi: number | null;
  serviceUuids: string[];
  verified: boolean;
  serviceVerified: boolean;
  characteristicsVerified: boolean;
  matchReason: string;
  reason: string;
  advanced: boolean;
};

export type WifiDevice = {
  host: string;
  method: string;
  confirmed: boolean;
  response: string;
  error: string;
};

export type DiscoverySnapshot = {
  bleState: string;
  bleResults: BleDevice[];
  wifiState: string;
  wifiResults: WifiDevice[];
};

export type BackendSnapshotPayload = {
  connection: ConnectionSnapshot;
  frame: FrameSnapshot;
  matrix: MatrixSnapshot;
  selection: SelectionSnapshot;
  display: DisplaySnapshot;
  baseline: Record<string, unknown>;
  commands: Record<string, unknown>;
  logs: LogsSnapshot;
  discovery: DiscoverySnapshot;
  diagnostics: Record<string, unknown>;
};

export type HistoryPoint = {
  seq: number;
  timeSeconds: number | null;
  value: number | null;
};

export type HistorySeries = {
  cell: string;
  points: HistoryPoint[];
};

export type HistoryPayload = {
  selectionRevision: number;
  title: string;
  unit: string;
  revision: number;
  series: HistorySeries[];
};

export type SnapshotMessage = {
  type: "snapshot";
  timeMs: number;
  payload: BackendSnapshotPayload;
};

export type LogMessage = {
  type: "log";
  level: string;
  message: string;
  timeMs: number;
};

export type ErrorMessage = {
  type: "error";
  scope: string;
  message: string;
  detail?: string;
};

export type DiscoveryMessage = {
  type: "discovery";
  mode: "ble" | "wifi";
  payload: BleDevice[] | WifiDevice[];
};

export type HistoryMessage = {
  type: "history";
  payload: HistoryPayload;
};

export type WebSocketMessage = SnapshotMessage | LogMessage | ErrorMessage | DiscoveryMessage | HistoryMessage;

export type SerialPort = {
  device: string;
  name: string;
  description: string;
  hwid: string;
  label: string;
  value: string;
};

export type DesktopBridge = {
  getBackendUrl: () => Promise<string>;
  selectReplayFile: () => Promise<string | null>;
};

declare global {
  interface Window {
    sensorarrayDesktop?: DesktopBridge;
  }
}

