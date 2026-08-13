export type TransportMode = "serial" | "ble" | "wifi" | "replay";
export type DisplayMode = "absolute_pf" | "delta_percent";
export type CommandLineEnding = "lf" | "crlf" | "none";
export type MeasurementMode = "CAP" | "VOLT" | "RES";
export type RowMeasurementMode = MeasurementMode;
export type FrameLayout = "HOMOGENEOUS" | "MIXED";
export type MeasurementQuantity = "capacitance" | "voltage" | "resistance";
export type MeasurementUnit = "pF" | "V" | "ohm";
export type MatrixSnapshotMode = MeasurementMode | "MIXED";
export type MatrixSnapshotQuantity = MeasurementQuantity | "mixed" | "row_specific";
export type MatrixSnapshotUnit = MeasurementUnit | "%" | "";
export type MeasurementTransitionState =
  | "applied"
  | "requested"
  | "accepted"
  | "configuring_rail"
  | "timeout"
  | "error"
  | "synced";

export type VoltageRailSnapshot = {
  configured: boolean;
  state: string;
  requestId: number | null;
  measuredAvddV: number | null;
  measuredAvssV: number | null;
};

export type RailTelemetry = {
  railSpanUv: number | null;
  valid: boolean | null;
  fresh: boolean | null;
  age: number | null;
  ageSeconds?: number | null;
  source: string;
  reason: string;
  timestamp: number | null;
};

export type RowModeProfileSnapshot = {
  appliedModes: RowMeasurementMode[];
  pendingModes: RowMeasurementMode[] | null;
  transitionState: MeasurementTransitionState;
  requestId: number | null;
  generation: number | null;
  frameSeq: number | null;
  error: string;
};

export type MeasurementSnapshot = {
  appliedMode: MeasurementMode;
  pendingMode: MeasurementMode | null;
  transitionState: MeasurementTransitionState;
  requestId: number | null;
  generation: number | null;
  frameSeq: number | null;
  error: string;
  deviceState?: string;
  rail: VoltageRailSnapshot;
  railTelemetry?: RailTelemetry;
  rowProfile?: RowModeProfileSnapshot;
};

export type ConnectionSnapshot = {
  transportMode?: TransportMode;
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
  hostParserFps?: number;
  generation?: number | null;
  requestId?: number | null;
  layout?: FrameLayout;
  rowModes?: RowMeasurementMode[];
  profileGeneration?: number | null;
  profileRequestId?: number | null;
};

export type MatrixDiagnostics = {
  reference?: string | null;
  railValid?: boolean | null;
  railAgeFrames?: number | null;
  avddUv?: number | null;
  avssUv?: number | null;
  matrixReferenceUv?: number | null;
  referenceResistorOhms?: number | null;
  durationUs?: number | null;
  transitionDurationUs?: number | null;
  gainChangeCount?: number | null;
  overrangeCount?: number | null;
  autorangeAttemptCount?: number | null;
  autorangeFallbackCount?: number | null;
  recoveredRetryCount?: number | null;
  drdyTimeoutCount?: number | null;
  staleCount?: number | null;
  spiErrorCount?: number | null;
  [key: string]: unknown;
};

export type MatrixSnapshot = {
  rows: string[];
  cols: string[];
  quantity: MatrixSnapshotQuantity;
  mode?: MatrixSnapshotMode;
  unit: MatrixSnapshotUnit;
  wireUnit?: MeasurementUnit;
  scale: number;
  format?: string;
  values: (number | null)[][];
  displayValues: (number | null)[][];
  rawFixed: (number | null)[][];
  valid: boolean[][];
  fresh: boolean[][];
  error?: boolean[][];
  errorCodes: (number | null)[][];
  errorReasons: (string | null)[][];
  pga: (number | null)[][];
  pgaBypass: boolean[][];
  sourceTransport: string;
  generation: number | null;
  requestId: number | null;
  diagnostics: MatrixDiagnostics;
  rawHeader?: string;
  rawTrailer?: string;
  correctedPf: (number | null)[][];
  rawPf: (number | null)[][];
  userOffsetPf: (number | null)[][];
  validMask: boolean[][];
  domain: string;
  modeByRow?: RowMeasurementMode[];
  unitByRow?: (MeasurementUnit | "%")[];
  scaleByRow?: number[];
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
    quantity?: MeasurementQuantity;
  };
  colourRanges?: Partial<Record<ColourDomain, ColourRangeSnapshot>>;
};

export type ColourDomain = "cap_absolute" | "cap_delta" | "voltage" | "resistance";

export type ColourRangeSnapshot = {
  min: number | null;
  max: number | null;
  frozen: boolean;
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

export type BleScanResponse = {
  ok: boolean;
  devices: BleDevice[];
  advancedDevices?: BleDevice[];
  error?: string;
  state?: string;
  durationMs?: number;
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
  measurement: MeasurementSnapshot;
  frame: FrameSnapshot;
  matrix: MatrixSnapshot;
  capacitance?: CapacitanceSnapshot;
  selection: SelectionSnapshot;
  display: DisplaySnapshot;
  baseline: BaselineSnapshot;
  commands: Record<string, unknown>;
  logs: LogsSnapshot;
  discovery: DiscoverySnapshot;
  diagnostics: Record<string, unknown>;
  battery?: BatterySnapshot;
  ads?: AdsSnapshot;
  rates?: RateSnapshot;
};

export type CapacitanceSnapshot = {
  available: boolean;
  rawPf: (number | null)[][];
  correctedPf: (number | null)[][];
  userOffsetPf: (number | null)[][];
  displayPf: (number | null)[][];
  displayMode: DisplayMode;
};

export type HistoryPoint = {
  seq: number;
  timeSeconds: number | null;
  value: number | null;
  valid?: boolean;
  fresh?: boolean;
};

export type HistorySeries = {
  cell: string;
  points: HistoryPoint[];
};

export type HistoryPayload = {
  mode: MeasurementMode;
  quantity: MeasurementQuantity;
  selectionRevision: number;
  title: string;
  unit: string;
  revision: number;
  latestN?: number;
  series: HistorySeries[];
};

export type BatterySnapshot = {
  revision?: number;
  available?: boolean;
  state?: string;
  batteryText?: string;
  batteryMv?: number | null;
  batteryState?: string;
  valid?: boolean | null;
  fresh?: boolean | null;
  ageMs?: number | null;
  ageFrames?: number | null;
  ageSeconds?: number | null;
  reason?: string;
  restoreResult?: string | null;
  restoreFailureCount?: number | null;
  railUv?: number | null;
  railValid?: boolean | null;
  railState?: string | null;
  railErrorUv?: number | null;
  retryCount?: number | null;
  retryLimit?: number | null;
  retryLastCount?: number | null;
  retryTotalCount?: number | null;
  unstableCount?: number | null;
  timeoutCount?: number | null;
  spreadRaw?: number | null;
  spreadMaximumRaw?: number | null;
  validRunCount?: number | null;
  invalidRunCount?: number | null;
  rawFields?: Record<string, string>;
  latestAttempt?: BatteryTelemetryAttempt | null;
  lastGood?: BatteryTelemetryAttempt | null;
  [key: string]: unknown;
};

export type BatteryTelemetryAttempt = {
  batteryMv?: number | null;
  batteryText?: string;
  valid?: boolean | null;
  fresh?: boolean | null;
  ageMs?: number | null;
  ageSeconds?: number | null;
  reason?: string;
  source?: string;
  firmwareAuthoritative?: boolean;
  timestamp?: number | null;
  [key: string]: unknown;
};

export type AdsSnapshot = {
  identity?: { chip?: string; valid?: string | number | boolean; [key: string]: unknown };
  identityAvailable?: boolean;
  identityConfirmed?: boolean | null;
  label?: string;
  chip?: string;
  valid?: boolean;
  state?: string;
  requestId?: number | null;
  sampleCount?: number | null;
  freshCount?: number | null;
  period?: number | null;
  spiError?: string | number | null;
  drdyError?: string | number | null;
  restore?: string | boolean | null;
  error?: string;
  [key: string]: unknown;
};

export type RateSnapshot = {
  captureFps?: number | null;
  emittedFps?: number | null;
  serialOutputFps?: number | null;
  bleOutputFps?: number | null;
  wifiOutputFps?: number | null;
  targetFps?: number | null;
  hostParserFps?: number | null;
  [key: string]: unknown;
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

export type MeasurementModeRequest = {
  mode: MeasurementMode;
  measuredAvddV?: number;
  measuredAvssV?: number;
};

export type MeasurementModeResponse = {
  ok: boolean;
  measurement?: MeasurementSnapshot;
  error?: string;
};

export type RowModesRequest = {
  modes: RowMeasurementMode[];
};

export type RowModesResponse = {
  ok: boolean;
  modes?: RowMeasurementMode[];
  measurement?: MeasurementSnapshot;
  error?: string;
};

export type OffsetScope = "cell" | "row" | "all";

export type OffsetResponse = {
  ok: boolean;
  offsetsPf: number[][];
  changedCells?: number;
};

export type SessionDataFormat = "csv" | "xlsx" | "mat" | "h5";

export type SetupProfile = {
  schemaVersion: 1 | 2 | 3;
  appVersion?: string;
  transport: {
    mode: TransportMode;
    serial: { port?: string; baud: number };
    wifi: { host?: string; fallbackHost?: string };
    ble: { address?: string; deviceId?: string };
    replay: { path?: string; speed: number };
  };
  acquisition: { rows: number; measurementMode: MeasurementMode; rowModes: RowMeasurementMode[] };
  voltageRail: {
    measuredAvddV: number | null;
    measuredAvssV: number | null;
  };
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

export type SerialPortsResponse = {
  ok: boolean;
  ports: SerialPort[];
  error?: string;
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
  onMenuImportSetup: (callback: () => void) => () => void;
  onMenuExportSetup: (callback: () => void) => () => void;
  onMenuImportData: (callback: () => void) => () => void;
  onMenuExportData: (callback: () => void) => () => void;
  onScreenshotResult: (callback: (result: DesktopActionResult) => void) => () => void;
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
