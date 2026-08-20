from __future__ import annotations

from abc import ABC, abstractmethod


class TransportError(RuntimeError):
    """Base class for typed transport failures."""


class TransportNotSent(TransportError):
    """The transport proved that no command bytes were submitted."""


class TransportWriteOutcomeUnknown(TransportError):
    """Bytes may have reached firmware; the command must not be retried."""


class TransportShutdownTimeout(TransportError):
    """A transport worker/client did not fully stop inside its bound."""


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
