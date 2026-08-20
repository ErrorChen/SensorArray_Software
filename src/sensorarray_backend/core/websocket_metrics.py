from __future__ import annotations

import threading


class WebSocketMetrics:
    """Process-local connection accounting used for leak diagnostics."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = 0
        self._accepted = 0

    def connected(self) -> None:
        with self._lock:
            self._active += 1
            self._accepted += 1

    def disconnected(self) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {"activeSubscribers": self._active, "acceptedConnections": self._accepted}


websocket_metrics = WebSocketMetrics()


__all__ = ["WebSocketMetrics", "websocket_metrics"]
