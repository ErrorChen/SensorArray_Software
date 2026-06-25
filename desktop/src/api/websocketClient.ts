import type { WebSocketMessage } from "./types";

export type WebSocketHandlers = {
  onMessage: (message: WebSocketMessage) => void;
  onConnectionStatus: (status: "connecting" | "connected" | "disconnected" | "reconnecting") => void;
};

export class SnapshotWebSocket {
  private socket: WebSocket | null = null;
  private stopped = false;
  private reconnectTimer: number | null = null;

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
    const url = this.baseUrl.replace(/^http/, "ws");
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
      this.socket?.close();
    };
  }
}
