from __future__ import annotations

import threading


class SessionManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._generation = 0

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def next_generation(self) -> int:
        with self._lock:
            self._generation += 1
            return self._generation
