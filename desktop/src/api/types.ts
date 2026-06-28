export type TransportMode = "serial" | "ble" | "wifi" | "replay";
export type DisplayMode = "absolute_pf" | "delta_percent";
export type CommandLineEnding = "lf" | "crlf" | "none";

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
  userOffsetPf: (number | null)[][];
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
  pendingDisplayMode?: DisplayMode | null;
  measurementDomain: string;
  showCellText: boolean;
  pauseDisplay: boolean;
  freezeColor: boolean;
  unitMode: string;
  circuitOffsetPf: number;
  trendLatestN?: number;
  colorRange: {
    min: number | null;
    max: number | null;
    frozen: boolean;
  };
};

export type BaselineSnapshot = {
  ok?: boolean;
  status?: "idle" | "capturing" | "ready" | "invalid" | "no_data" | "reset" | string;
  label?: string;
  invalidReason?: string;
  progress?: number;
  ready?: boolean;
  validCells?: number;
  frameCount?: number;
  rejectedFrameCount?: number;
  pendingDisplayMode?: DisplayMode | null;
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
  baseline: BaselineSnapshot;
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
  latestN?: number;
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

export type WriteCommandRequest = {
  text: string;
  lineEnding: CommandLineEnding;
  encoding: "utf-8";
  mode: "text";
};

export type WriteCommandResponse = {
  ok: boolean;
  transport?: TransportMode | string;
  bytesWritten?: number;
  error?: string;
};

export type RowsResponse = {
  ok: boolean;
  requestedRows: number;
  appliedRows: number;
  displayOnly: boolean;
  activeTransport: string;
  status: string;
  rows: number;
  applied: boolean;
};

export type OffsetScope = "cell" | "row" | "all";

export type OffsetResponse = {
  ok: boolean;
  offsetsPf: number[][];
  changedCells?: number;
};

export type SessionDataFormat = "csv" | "xlsx" | "mat" | "h5";

export type SetupProfile = {
  schemaVersion: 1;
  appVersion?: string;
  transport: {
    mode: TransportMode;
    serial: { port?: string; baud: number };
    wifi: { host?: string; fallbackHost?: string };
    ble: { address?: string; deviceId?: string };
    replay: { path?: string; speed: number };
  };
  acquisition: { rows: number };
  display: {
    displayMode: DisplayMode;
    measurementDomain: string;
    showCellText: boolean;
    pauseDisplay: boolean;
    freezeColor: boolean;
    unitMode: string;
    circuitOffsetPf: number;
    trendLatestN?: number;
  };
  offsetsPf: number[][];
  command: { lineEnding: CommandLineEnding };
  paths: { defaultSaveDirectory: string };
};

export type SetupProfileApplyResponse = {
  ok: boolean;
  profile: SetupProfile;
  warnings: string[];
};

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
  getRuntimeDirectory: () => Promise<string>;
  getDefaultSaveDirectory: () => Promise<string>;
  setDefaultSaveDirectory: (directory: string) => Promise<PathCheckResult>;
  selectDefaultSaveDirectory: () => Promise<PathCheckResult & { canceled?: boolean }>;
  openDefaultSaveDirectory: () => Promise<PathCheckResult>;
  selectReplayFile: () => Promise<string | null>;
  selectSessionDataFile: () => Promise<string | null>;
  selectSetupProfile: () => Promise<string | null>;
  readTextFile: (path: string) => Promise<string>;
  onImportReplayData: (callback: (path: string) => void) => () => void;
  onImportSessionData: (callback: () => void) => () => void;
  onExportSessionData: (callback: () => void) => () => void;
  onImportSetupProfile: (callback: () => void) => () => void;
  onExportSetupProfile: (callback: () => void) => () => void;
  onCaptureScreenshot: (callback: () => void) => () => void;
  chooseSessionExportPath: (defaultName: string) => Promise<DesktopActionResult>;
  saveExportedSession: (defaultName: string, data: ArrayBuffer | Uint8Array | string) => Promise<DesktopActionResult>;
  writeBinaryFile: (path: string, data: ArrayBuffer | Uint8Array | string) => Promise<DesktopActionResult>;
  saveSetupProfile: (defaultName: string, data: string) => Promise<DesktopActionResult>;
  captureScreenshot: () => Promise<DesktopActionResult>;
};

export type DesktopActionResult = {
  ok: boolean;
  path?: string;
  error?: string;
  canceled?: boolean;
};

export type PathCheckResult = {
  ok: boolean;
  path: string;
  error?: string;
};

declare global {
  interface Window {
    sensorarrayDesktop?: DesktopBridge;
  }
}
