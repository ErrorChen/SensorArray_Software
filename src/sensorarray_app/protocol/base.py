from __future__ import annotations

from typing import Protocol

from sensorarray_app.domain.models import DomainEvent, TransportEnvelope


class Protocol(Protocol):
    name: str

    def feed(self, envelope: TransportEnvelope) -> list[DomainEvent]:
        ...
