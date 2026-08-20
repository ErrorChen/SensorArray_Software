from __future__ import annotations

import csv
import math

import numpy as np

from sensorarray_app.domain.models import ParserErrorEvent, TransportEnvelope, VoltageFrame

MATV_TYPES = {"MATV", "MATV_RAW", "MATV_GAIN", "MATV_ERR"}


class LegacyMatvProtocol:
    name = "LegacyMatvProtocol"

    def parse_line(self, line: str, envelope: TransportEnvelope) -> VoltageFrame | ParserErrorEvent | None:
        # This adapter is a compatibility probe behind the current ASCII
        # router. An unrelated firmware diagnostic may contain CSV-special
        # characters; it must not become a legacy MATV parser rejection.
        # Confirm the literal legacy tag before asking csv.reader to parse it.
        candidate_tag = line.split(",", maxsplit=1)[0].strip()
        if candidate_tag not in MATV_TYPES:
            return None
        try:
            fields = next(csv.reader([line], strict=True))
        except csv.Error as exc:
            return ParserErrorEvent(envelope.source, envelope.channel, "matv_csv", str(exc), envelope.sessionGeneration, line)
        if not fields:
            return None
        tag = fields[0].strip()
        if tag.endswith("_HEADER"):
            return None
        try:
            seq = int(fields[1])
            timestamp_us = int(fields[2])
        except (IndexError, ValueError) as exc:
            return ParserErrorEvent(envelope.source, envelope.channel, "matv_header", str(exc), envelope.sessionGeneration, line)
        duration_us = 0
        unit = "uV"
        start = 3
        if tag == "MATV":
            duration_us = int(fields[3])
            unit = fields[4].strip() or "uV"
            start = 5
        values = []
        for item in fields[start : start + 64]:
            try:
                values.append(float(item))
            except ValueError:
                values.append(math.nan)
        if len(values) != 64:
            return ParserErrorEvent(envelope.source, envelope.channel, "matv_count", "expected 64 values", envelope.sessionGeneration, line)
        arr = np.asarray(values, dtype=np.float64)
        valid = np.isfinite(arr)
        if unit.lower() == "mv":
            arr *= 1_000.0
        elif unit.lower() == "v":
            arr *= 1_000_000.0
        return VoltageFrame(
            seq=seq,
            timestampUs=timestamp_us,
            durationUs=duration_us,
            valuesUv=arr,
            validMask=valid,
            sourceTransport=envelope.source,
            sessionGeneration=envelope.sessionGeneration,
            receivedTime=envelope.receivedWallTime,
            frameType=tag,
        )
