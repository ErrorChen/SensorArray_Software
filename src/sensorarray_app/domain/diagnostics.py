from __future__ import annotations

from dataclasses import dataclass
from time import monotonic


@dataclass
class RateCounter:
    count: int = 0
    start: float = 0.0

    def tick(self, amount: int = 1) -> None:
        if self.start <= 0:
            self.start = monotonic()
        self.count += int(amount)

    def fps(self) -> float:
        if self.start <= 0:
            return 0.0
        elapsed = max(1e-6, monotonic() - self.start)
        return self.count / elapsed
