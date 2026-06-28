import type {
  BaselineSnapshot,
  BleDevice,
  DisplayMode,
  HistoryPayload,
  OffsetResponse,
  OffsetScope,
  RowsResponse,
  SerialPort,
  SessionDataFormat,
  SetupProfile,
  SetupProfileApplyResponse,
  TransportMode,
  WifiDevice,
  WriteCommandRequest,
  WriteCommandResponse
} from "./types";

export class BackendHttpClient {
  constructor(private readonly baseUrl: string) {}

  async setMode(mode: TransportMode): Promise<void> {
    await this.post("/api/transport/mode", { mode });
  }

  async listSerialPorts(): Promise<SerialPort[]> {
    const payload = await this.get<{ ports: SerialPort[] }>("/api/transport/serial/ports");
    return payload.ports;
  }

  async connectSerial(port: string, baud: number): Promise<void> {
    await this.post("/api/transport/serial/connect", { port, baud });
  }

  async scanBle(): Promise<BleDevice[]> {
    const payload = await this.get<{ devices: BleDevice[] }>("/api/transport/ble/scan");
    return payload.devices;
  }

  async connectBle(address: string, deviceId = ""): Promise<void> {
    await this.post("/api/transport/ble/connect", { address, deviceId });
  }

  async discoverWifi(): Promise<WifiDevice[]> {
    const payload = await this.get<{ devices: WifiDevice[] }>("/api/transport/wifi/discover");
    return payload.devices;
  }

  async connectWifi(host: string): Promise<void> {
    await this.post("/api/transport/wifi/connect", { host });
  }

  async disconnect(): Promise<void> {
    await this.post("/api/transport/disconnect", {});
  }

  async writeCommand(request: WriteCommandRequest): Promise<WriteCommandResponse> {
    return this.post<WriteCommandResponse>("/api/transport/write", request);
  }

  async openReplay(path: string, speed: number): Promise<void> {
    await this.post("/api/replay/open", { path, speed });
  }

  async startReplay(): Promise<void> {
    await this.post("/api/replay/start", {});
  }

  async stopReplay(): Promise<void> {
    await this.post("/api/replay/stop", {});
  }

  async setRows(rows: number): Promise<RowsResponse> {
    return this.post<RowsResponse>("/api/rows", { rows });
  }

  async setDisplaySettings(settings: {
    displayMode?: DisplayMode;
    measurementDomain?: string;
    showCellText?: boolean;
    pauseDisplay?: boolean;
    freezeColor?: boolean;
    unitMode?: string;
    circuitOffsetPf?: number;
  }): Promise<Record<string, unknown>> {
    return this.post<Record<string, unknown>>("/api/settings/display", settings);
  }

  async baseline(action: "capture" | "reset" | "cancel"): Promise<BaselineSnapshot> {
    return this.post<BaselineSnapshot>("/api/settings/baseline", { action });
  }

  async selectCell(cell: string): Promise<Record<string, unknown>> {
    return this.post<Record<string, unknown>>("/api/selection", { cell });
  }

  async getHistory(latestN: number): Promise<HistoryPayload> {
    return this.get<HistoryPayload>(`/api/history?latest_n=${encodeURIComponent(String(latestN))}`);
  }

  async getOffsets(): Promise<OffsetResponse> {
    return this.get<OffsetResponse>("/api/settings/offsets");
  }

  async setOffsetCell(row: number, col: number, offsetPf: number): Promise<OffsetResponse> {
    return this.post<OffsetResponse>("/api/settings/offsets/cell", { row, col, offsetPf });
  }

  async clearOffsets(scope: OffsetScope, row?: number, col?: number): Promise<OffsetResponse> {
    return this.post<OffsetResponse>("/api/settings/offsets/clear", { scope, row, col });
  }

  async zeroCurrentOffsets(scope: OffsetScope, row?: number, col?: number): Promise<OffsetResponse> {
    return this.post<OffsetResponse>("/api/settings/offsets/zero-current", { scope, row, col });
  }

  async exportSession(format: SessionDataFormat): Promise<ArrayBuffer> {
    const response = await fetch(`${this.baseUrl}/api/export/session?format=${encodeURIComponent(format)}`);
    if (!response.ok) {
      const text = await response.text();
      throw new Error(text || `HTTP ${response.status}`);
    }
    return response.arrayBuffer();
  }

  async importSession(path: string): Promise<Record<string, unknown>> {
    return this.post<Record<string, unknown>>("/api/import/session", { path });
  }

  async getSetupProfile(): Promise<SetupProfile> {
    return this.get<SetupProfile>("/api/setup/profile");
  }

  async applySetupProfile(profile: SetupProfile): Promise<SetupProfileApplyResponse> {
    return this.post<SetupProfileApplyResponse>("/api/setup/profile", profile);
  }

  private async get<T>(path: string): Promise<T> {
    const response = await fetch(`${this.baseUrl}${path}`);
    return this.decode<T>(response);
  }

  private async post<T = unknown>(path: string, body: unknown): Promise<T> {
    const response = await fetch(`${this.baseUrl}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    return this.decode<T>(response);
  }

  private async decode<T>(response: Response): Promise<T> {
    if (!response.ok) {
      const text = await response.text();
      throw new Error(text || `HTTP ${response.status}`);
    }
    return (await response.json()) as T;
  }
}
