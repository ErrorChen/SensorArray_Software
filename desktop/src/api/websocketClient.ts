import type { WebSocketMessage } from "./types";
import { normaliseBackendUrl } from "./backendUrl";

export type WebSocketHandlers = {
  onMessage: (message: WebSocketMessage) => void;
  onConnectionStatus: (status: "connecting" | "connected" | "disconnected" | "reconnecting") => void;
  onError?: (message: string) => void;
};

export class SnapshotWebSocket {
  private socket: WebSocket | null = null;
  private stopped = false;
  private reconnectTimer: number | null = null;
  private lastErrorAt = 0;

  constructor(
    private readonly baseUrl: string,
    private readonly handlers: WebSocketHandlers
  ) {}

  start(): void {
    this.stopped = false;
    this.open();
  }

  stop(): void {
    this.stopped = true;
    if (this.reconnectTimer !== null) {
      window.clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.socket?.close();
    this.socket = null;
  }

  private open(): void {
    this.handlers.onConnectionStatus(this.socket ? "reconnecting" : "connecting");
    const url = toWebSocketUrl(this.baseUrl);
    this.socket = new WebSocket(`${url}/ws`);
    this.socket.onopen = () => this.handlers.onConnectionStatus("connected");
    this.socket.onmessage = (event) => {
      const message = JSON.parse(event.data) as WebSocketMessage;
      this.handlers.onMessage(message);
    };
    this.socket.onclose = () => {
      this.handlers.onConnectionStatus("disconnected");
      if (!this.stopped) {
        this.reconnectTimer = window.setTimeout(() => this.open(), 1000);
      }
    };
    this.socket.onerror = () => {
      this.notifyError(`Backend WebSocket unreachable at ${url}/ws`);
      this.socket?.close();
    };
  }

  private notifyError(message: string): void {
    const now = Date.now();
    if (now - this.lastErrorAt < 5000) {
      return;
    }
    this.lastErrorAt = now;
    this.handlers.onError?.(message);
  }
}

function toWebSocketUrl(baseUrl: string): string {
  const url = normaliseBackendUrl(baseUrl);
  return url.replace(/^http:/, "ws:").replace(/^https:/, "wss:");
}
