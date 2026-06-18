from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field


@dataclass
class StatisticsStore:
    transportBytes: int = 0
    transportPackets: int = 0
    parserFrames: int = 0
    parserRejects: int = 0
    crcFailures: int = 0
    sequenceGaps: int = 0
    fragmentDrops: int = 0
    hostQueueDrops: int = 0
    historyOverwrites: int = 0
    renderSkipped: int = 0
    reconnectCount: int = 0
    _samples: deque[tuple[float, str, int]] = field(default_factory=deque)
    byReason: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def record_transport(self, byte_count: int) -> None:
        self.transportBytes += int(byte_count)
        self.transportPackets += 1
        self._sample("transport", 1)

    def record_frame(self) -> None:
        self.parserFrames += 1
        self._sample("frame", 1)

    def record_reject(self, reason: str) -> None:
        self.parserRejects += 1
        self.byReason[reason] += 1

    def snapshot(self, visual_fps: float = 0.0, stored_fps: float = 0.0) -> dict:
        now = time.monotonic()
        while self._samples and self._samples[0][0] < now - 5.0:
            self._samples.popleft()
        duration = max(1e-6, now - self._samples[0][0]) if self._samples else 1.0
        parser_count = sum(amount for _, kind, amount in self._samples if kind == "frame")
        return {
            "transportBytes": self.transportBytes,
            "transportPackets": self.transportPackets,
            "parserFrames": self.parserFrames,
            "parserRejects": self.parserRejects,
            "crcFailures": self.crcFailures,
            "sequenceGaps": self.sequenceGaps,
            "fragmentDrops": self.fragmentDrops,
            "hostQueueDrops": self.hostQueueDrops,
            "historyOverwrites": self.historyOverwrites,
            "renderSkipped": self.renderSkipped,
            "visualFps": visual_fps,
            "parserFps": parser_count / duration,
            "storedFps": stored_fps,
            "reconnectCount": self.reconnectCount,
            "rejectsByReason": dict(self.byReason),
        }

    def _sample(self, kind: str, amount: int) -> None:
        self._samples.append((time.monotonic(), kind, int(amount)))
