from __future__ import annotations

from abc import ABC, abstractmethod


class Transport(ABC):
    source: str

    @abstractmethod
    def start(self) -> None:
        ...

    @abstractmethod
    def stop(self) -> None:
        ...

    def send_command(self, command: str) -> None:
        raise NotImplementedError

    def write(self, data: bytes) -> int:
        raise NotImplementedError
