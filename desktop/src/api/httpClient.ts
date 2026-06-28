import type {
  BaselineSnapshot,
  BleScanResponse,
  DisplayMode,
  HistoryPayload,
  OffsetResponse,
  OffsetScope,
  RowsResponse,
  SerialPortsResponse,
  SessionDataFormat,
  SetupProfile,
  SetupProfileApplyResponse,
  TransportMode,
  WifiDevice,
  WriteCommandRequest,
  WriteCommandResponse
} from "./types";
import { normaliseBackendUrl } from "./backendUrl";

export class BackendHttpClient {
  private readonly baseUrl: string;

  constructor(baseUrl: string) {
    this.baseUrl = normaliseBackendUrl(baseUrl);
  }

  async setMode(mode: TransportMode): Promise<void> {
    await this.post("/api/transport/mode", { mode });
  }

  async listSerialPorts(): Promise<SerialPortsResponse> {
    const payload = await this.get<SerialPortsResponse>("/api/transport/serial/ports");
    return { ok: payload.ok !== false, ports: payload.ports ?? [], error: payload.error ?? "" };
  }

  async connectSerial(port: string, baud: number): Promise<void> {
    await this.post("/api/transport/serial/connect", { port, baud });
  }

  async scanBle(timeoutSeconds = 10): Promise<BleScanResponse> {
    const payload = await this.get<BleScanResponse>(`/api/transport/ble/scan?timeout=${encodeURIComponent(String(timeoutSeconds))}`);
    return {
      ok: payload.ok !== false,
      devices: payload.devices ?? [],
      advancedDevices: payload.advancedDevices ?? [],
      error: payload.error ?? "",
      state: payload.state,
      durationMs: payload.durationMs
    };
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
    const url = this.url(`/api/export/session?format=${encodeURIComponent(format)}`);
    let response: Response;
    try {
      response = await fetch(url);
    } catch (error) {
      throw new Error(formatBackendError(this.baseUrl, `/api/export/session`, error));
    }
    await assertHttpOk(response, url);
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
    const url = this.url(path);
    let response: Response;
    try {
      response = await fetch(url);
    } catch (error) {
      throw new Error(formatBackendError(this.baseUrl, path, error));
    }
    return this.decode<T>(response, url);
  }

  private async post<T = unknown>(path: string, body: unknown): Promise<T> {
    const url = this.url(path);
    let response: Response;
    try {
      response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      });
    } catch (error) {
      throw new Error(formatBackendError(this.baseUrl, path, error));
    }
    return this.decode<T>(response, url);
  }

  private async decode<T>(response: Response, url: string): Promise<T> {
    await assertHttpOk(response, url);
    return (await response.json()) as T;
  }

  private url(path: string): string {
    if (!this.baseUrl) {
      throw new Error("Backend URL is not resolved");
    }
    if (this.baseUrl.includes(":6666")) {
      throw new Error("Legacy unsafe backend port 6666 is still in use; check backend port policy.");
    }
    return `${this.baseUrl}${path}`;
  }
}

export function formatBackendError(baseUrl: string, path: string, error: unknown): string {
  if (!baseUrl) {
    return "Backend URL is not resolved";
  }
  const url = `${baseUrl}${path}`;
  if (baseUrl.includes(":6666")) {
    return "Legacy unsafe backend port 6666 is still in use; check backend port policy.";
  }
  const message = error instanceof Error ? error.message : String(error);
  return `Backend unreachable at ${url}: ${message}`;
}

async function assertHttpOk(response: Response, url: string): Promise<void> {
  if (response.ok) {
    return;
  }
  const detail = await responseDetail(response);
  throw new Error(`Backend HTTP ${response.status} at ${url}: ${detail || response.statusText || "request failed"}`);
}

async function responseDetail(response: Response): Promise<string> {
  const text = await response.text();
  if (!text) {
    return "";
  }
  try {
    const parsed = JSON.parse(text) as { detail?: unknown; error?: unknown; message?: unknown };
    const detail = parsed.detail ?? parsed.error ?? parsed.message;
    return typeof detail === "string" ? detail : JSON.stringify(detail ?? parsed);
  } catch {
    return text;
  }
}
