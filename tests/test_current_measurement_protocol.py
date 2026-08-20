from __future__ import annotations

import time
import zlib
from pathlib import Path

import numpy as np
import pytest

from sensorarray_app.domain.models import (
    BatteryTelemetry,
    CapacitanceFrame,
    LogRecord,
    MeasurementFrame,
    ParserErrorEvent,
    TransportEnvelope,
    VoltageFrame,
)
from sensorarray_app.protocol.crc import crc32_reflected
from sensorarray_app.protocol.measurement_ascii import MeasurementAsciiParser
from sensorarray_app.protocol.registry import ProtocolRegistry


FIXTURES = Path(__file__).parent / "fixtures" / "current_protocol"


def envelope(payload: bytes, channel: str = "data", source: str = "replay", generation: int = 3) -> TransportEnvelope:
    return TransportEnvelope(
        source=source,
        channel=channel,
        deviceId="CURRENT_PROTOCOL_TEST",
        sessionGeneration=generation,
        receivedMonotonicNs=time.monotonic_ns(),
        receivedWallTime=time.time(),
        rawPayload=payload,
    )


def build_measurement_packet(
    tag: str,
    rows: int,
    *,
    seq: int = 100,
    generation: int = 9,
    requestId: int = 77,
    values: list[str] | None = None,
    gains: list[int] | None = None,
    freshBits: int | None = None,
) -> bytes:
    """Mirror firmware tools/test_text_protocol.py's golden packet builder."""

    cells = rows * 8
    values = values or [str(index - 10) for index in range(cells)]
    gains = gains or [1, 2, 4, 8, 16, 32, 0, 1] * rows
    assert len(values) == cells
    assert len(gains) == cells
    validBits = sum(1 << index for index, value in enumerate(values) if not value.upper().startswith("X"))
    errorBits = ((1 << cells) - 1) ^ validBits
    if freshBits is None:
        freshBits = (1 << cells) - 1
    expectedBits = (1 << cells) - 1
    acquiredBits = expectedBits
    mode, unit, scale, frameFormat, reference = (
        ("VOLT", "V", -6, "uv", "AVDD_AVSS")
        if tag == "V"
        else ("RES", "ohm", -3, "mohm", "INTREF")
    )
    lines = [
        f"{tag},seq={seq},ts=987654,rows={rows},cells={cells},gen={generation},rid={requestId},"
        f"mode={mode},unit={unit},scale={scale},valid={validBits:016X},fresh={freshBits:016X},"
        f"error={errorBits:016X},expected={expectedBits:016X},acquired={acquiredBits:016X},"
        f"ref={reference},rail=1,age=2,avdd=3391000,avss=-2500000,"
        f"vexc=2500000,rref=100000,dur=12345,tr=234,gc=2,ov=1,aa={cells},fb=0,ir=1,to=0,"
        f"st=0,spi=0,fmt={frameFormat}-x,n={cells},bad={cells - validBits.bit_count()}"
    ]
    for chunkIndex in range((cells + 15) // 16):
        start = chunkIndex * 16
        lines.append(f"D{chunkIndex}," + ",".join(values[start : start + 16]))
    for chunkIndex in range((cells + 15) // 16):
        start = chunkIndex * 16
        packed = "".join(f"{gain:02X}" for gain in gains[start : start + 16])
        lines.append(f"P{chunkIndex},{packed}")
    payload = "".join(line + "\n" for line in lines).encode("ascii")
    lines.append(f"K,seq={seq},gen={generation},rid={requestId},crc={zlib.crc32(payload) & 0xFFFFFFFF:08X}")
    return "".join(line + "\n" for line in lines).encode("ascii")


def rebuild_crc(lines: list[bytes]) -> bytes:
    headerFields = dict(item.split("=", 1) for item in lines[0].decode().strip().split(",")[1:])
    crcPayload = b"".join(line.rstrip(b"\r\n") + b"\n" for line in lines[:-1])
    trailer = (
        f"K,seq={headerFields['seq']},gen={headerFields['gen']},rid={headerFields['rid']},"
        f"crc={zlib.crc32(crcPayload) & 0xFFFFFFFF:08X}\n"
    ).encode("ascii")
    return crcPayload + trailer


def frames(events: list[object]) -> list[MeasurementFrame]:
    return [event for event in events if isinstance(event, MeasurementFrame)]


def errors(events: list[object]) -> list[ParserErrorEvent]:
    return [event for event in events if isinstance(event, ParserErrorEvent)]


@pytest.mark.parametrize(
    ("fixture_path", "frame_type"),
    [
        (FIXTURES / "volt_rows2_mixed.txt", MeasurementFrame),
        (FIXTURES.parent / "b41" / "rows8_valid.txt", CapacitanceFrame),
    ],
)
def test_firmware_d4_runtime_diagnostic_can_interleave_without_corrupting_frame(fixture_path: Path, frame_type: type):
    lines = fixture_path.read_bytes().splitlines(keepends=True)
    diagnostic = b"D4,d=secondary,r=7,e=748159,mode=fast,k=FULL,statusFallbackUsed=1\n"
    events = ProtocolRegistry().feed(envelope(b"".join([lines[0], diagnostic, *lines[1:]])))

    assert len([event for event in events if isinstance(event, frame_type)]) == 1
    assert not errors(events)
    diagnostic_logs = [event for event in events if isinstance(event, LogRecord) and event.tag == "D4"]
    assert len(diagnostic_logs) == 1
    assert diagnostic_logs[0].parsedFields["d"] == "secondary"


def test_firmware_p5_performance_diagnostic_can_interleave_with_vr_pga_chunks():
    lines = (FIXTURES / "volt_rows2_mixed.txt").read_bytes().splitlines(keepends=True)
    diagnostic = b"P5,s=123,n=8,profile=fast_runtime,cnt=40,avg=900,max=1100,row=2,d=secondary\n"
    events = ProtocolRegistry().feed(envelope(b"".join([lines[0], diagnostic, *lines[1:]])))

    assert len(frames(events)) == 1
    assert not errors(events)
    diagnostic_logs = [event for event in events if isinstance(event, LogRecord) and event.tag == "P5"]
    assert len(diagnostic_logs) == 1
    assert diagnostic_logs[0].parsedFields["profile"] == "fast_runtime"


def test_embedded_cap_header_is_recovered_from_truncated_firmware_diagnostic():
    packet = (FIXTURES.parent / "b41" / "rows8_valid.txt").read_bytes()
    events = ProtocolRegistry().feed(envelope(b"PFU,d=s,r=4,arg0=acti" + packet))

    assert len([event for event in events if isinstance(event, CapacitanceFrame)]) == 1
    assert not errors(events)
    recovery = next(event for event in events if isinstance(event, LogRecord) and event.tag == "WIRE_INTERLEAVE")
    assert recovery.parsedFields == {
        "prefixTag": "PFU",
        "embeddedTag": "C",
        "embeddedSeq": "8",
        "droppedPendingFrame": "0",
        "activeFrameType": "NONE",
        "prefixLength": "21",
    }


def test_embedded_header_discards_only_unrecoverable_old_pending_frame():
    packet = (FIXTURES.parent / "b41" / "rows8_valid.txt").read_bytes()
    lines = packet.splitlines(keepends=True)
    corrupted = b"".join([lines[0], lines[1], lines[2][:40].rstrip(b"\n"), packet])
    events = ProtocolRegistry().feed(envelope(corrupted))

    recovered = [event for event in events if isinstance(event, CapacitanceFrame)]
    assert len(recovered) == 1
    assert recovered[0].seq == 8
    assert not errors(events)
    recovery = next(event for event in events if isinstance(event, LogRecord) and event.tag == "WIRE_INTERLEAVE")
    assert recovery.parsedFields["prefixTag"] == "D1"
    assert recovery.parsedFields["droppedPendingFrame"] == "1"
    assert recovery.parsedFields["activeFrameType"] == "C"


def test_firmware_derived_voltage_fixture_parses_negative_pga_masks_and_errors():
    packet = (FIXTURES / "volt_rows2_mixed.txt").read_bytes()
    parser = MeasurementAsciiParser()
    parsedFrames = frames(parser.feed(envelope(packet)))
    assert len(parsedFrames) == 1
    frame = parsedFrames[0]

    assert (frame.mode, frame.unit, frame.scale, frame.format) == ("VOLT", "V", -6, "uv-x")
    assert (frame.seq, frame.rows, frame.cells, frame.generation, frame.requestId) == (8, 2, 16, 7, 42)
    assert frame.rawFixedValues[0] == -1250
    assert frame.physicalValues[0] == pytest.approx(-0.00125)
    assert frame.physicalValues[5] == pytest.approx(1.0)
    assert np.isnan(frame.rawFixedValues[4])
    assert frame.errorCodes[4] == 0x03
    assert frame.errorReasons[4] == "ADS DRDY timeout"
    assert frame.errorCodes[8] == 0xFE
    assert frame.errorReasons[8] == "Unknown firmware cell error 0xFE"
    assert not frame.validMask[4]
    assert frame.validMask[9] and not frame.freshMask[9]
    assert frame.pgaValues.tolist() == [1, 2, 4, 8, 16, 32, 0, 1, 2, 4, 8, 16, 32, 0, 1, 2]
    assert frame.pgaBypassMask.tolist() == [False, False, False, False, False, False, True, False, False, False, False, False, False, True, False, False]
    assert frame.reference == "AVDD_AVSS"
    assert frame.railValid
    assert (frame.avddUv, frame.avssUv, frame.recoveredRetryCount) == (3_391_000, -2_500_000, 1)
    assert (frame.durationUs, frame.transitionDurationUs, frame.staleCount) == (12_000, 500, 1)
    assert frame.sourceTransport == "replay"
    assert frame.deviceId == "CURRENT_PROTOCOL_TEST"


def test_firmware_derived_resistance_fixture_uses_ohms_and_pga_bypass():
    parser = MeasurementAsciiParser()
    parsedFrames = frames(parser.feed(envelope((FIXTURES / "res_rows1_mixed.txt").read_bytes())))
    assert len(parsedFrames) == 1
    frame = parsedFrames[0]
    assert (frame.mode, frame.unit, frame.scale, frame.format) == ("RES", "ohm", -3, "mohm-x")
    assert frame.rawFixedValues[0] == 1000
    assert frame.physicalValues[0] == pytest.approx(1.0)
    assert frame.physicalValues[6] == pytest.approx(100_000.0)
    assert frame.pgaValues[0] == 0
    assert frame.pgaBypassMask[0]
    assert np.isnan(frame.physicalValues[2])
    assert frame.errorCodes[2] == 0x0D
    assert frame.errorReasons[2] == "Open circuit"


@pytest.mark.parametrize("fixtureName", ["volt_rows2_mixed.txt", "res_rows1_mixed.txt"])
def test_checked_in_golden_crc_matches_firmware_scope(fixtureName: str):
    lines = (FIXTURES / fixtureName).read_bytes().splitlines(keepends=True)
    crcPayload = b"".join(line.rstrip(b"\r\n") + b"\n" for line in lines[:-1])
    expectedCrc = int(lines[-1].decode().strip().rsplit("=", 1)[1], 16)
    assert crc32_reflected(crcPayload) == expectedCrc


def test_pga_chunk_is_inside_crc_scope_and_bad_frame_does_not_emit():
    packet = (FIXTURES / "volt_rows2_mixed.txt").read_bytes()
    corrupted = packet.replace(b"P0,010204", b"P0,020204")
    parser = MeasurementAsciiParser()
    events = parser.feed(envelope(corrupted))
    assert not frames(events)
    assert [event.reason for event in errors(events)] == ["crc"]
    assert parser.stats.crcFailures == 1


@pytest.mark.parametrize("rows", [1, 2, 4, 8])
def test_dynamic_rows_and_chunk_counts(rows: int):
    parser = MeasurementAsciiParser()
    parsedFrames = frames(parser.feed(envelope(build_measurement_packet("V", rows))))
    assert len(parsedFrames) == 1
    assert parsedFrames[0].rows == rows
    assert parsedFrames[0].cells == rows * 8
    assert len(parsedFrames[0].physicalValues) == rows * 8


@pytest.mark.parametrize(
    ("mutation", "expectedReason"),
    [
        (lambda packetLines: packetLines[:2] + [packetLines[1]] + packetLines[2:], "duplicate_d"),
        (lambda packetLines: [packetLines[0]] + packetLines[2:], "missing_d"),
        (lambda packetLines: packetLines[:2] + [b"D1,1,2,3,4,5,6,7,8\n"] + packetLines[2:], "extra_d"),
        (lambda packetLines: packetLines[:3] + [packetLines[2]] + packetLines[3:], "duplicate_p"),
        (lambda packetLines: packetLines[:2] + packetLines[3:], "missing_p"),
        (lambda packetLines: packetLines[:3] + [b"P1,0101010101010101\n"] + packetLines[3:], "extra_p"),
    ],
)
def test_duplicate_missing_and_extra_chunks_are_rejected(mutation, expectedReason: str):
    packetLines = build_measurement_packet("V", 1).splitlines(keepends=True)
    parser = MeasurementAsciiParser()
    events = parser.feed(envelope(b"".join(mutation(packetLines))))
    assert not frames(events)
    assert expectedReason in [event.reason for event in errors(events)]


@pytest.mark.parametrize(("field", "value"), [("seq", "101"), ("gen", "10"), ("rid", "78")])
def test_trailer_seq_generation_and_request_id_mismatch_rejects(field: str, value: str):
    packetLines = build_measurement_packet("V", 1).splitlines(keepends=True)
    trailer = packetLines[-1].decode("ascii")
    original = {"seq": "100", "gen": "9", "rid": "77"}[field]
    packetLines[-1] = trailer.replace(f"{field}={original}", f"{field}={value}").encode("ascii")
    parser = MeasurementAsciiParser()
    events = parser.feed(envelope(b"".join(packetLines)))
    assert not frames(events)
    assert "k_mismatch" in [event.reason for event in errors(events)]


def test_wrong_header_and_data_cell_counts_are_rejected():
    packet = build_measurement_packet("V", 1)
    badHeader = packet.replace(b"cells=8", b"cells=9", 1)
    headerEvents = MeasurementAsciiParser().feed(envelope(badHeader))
    assert "bad_cells" in [event.reason for event in errors(headerEvents)]

    packetLines = packet.splitlines(keepends=True)
    packetLines[1] = b"D0,1,2,3\n"
    dataEvents = MeasurementAsciiParser().feed(envelope(b"".join(packetLines)))
    assert "short_d_values" in [event.reason for event in errors(dataEvents)]


def test_header_masks_must_match_x_tokens_but_freshness_remains_independent():
    values = ["1", "X03", "3", "4", "5", "6", "7", "8"]
    packetLines = build_measurement_packet("V", 1, values=values, freshBits=0xFD).splitlines(keepends=True)
    packetLines[0] = packetLines[0].replace(b"valid=00000000000000FD", b"valid=00000000000000FF")
    packetLines[0] = packetLines[0].replace(b"bad=1", b"bad=0")
    parser = MeasurementAsciiParser()
    events = parser.feed(envelope(rebuild_crc(packetLines)))
    assert not frames(events)
    assert "valid_token_mismatch" in [event.reason for event in errors(events)]


def test_header_bad_count_must_match_valid_mask():
    packet = (FIXTURES / "volt_rows2_mixed.txt").read_bytes().replace(b"bad=2\n", b"bad=1\n")
    events = MeasurementAsciiParser().feed(envelope(packet))
    assert not frames(events)
    assert errors(events)[0].reason == "bad_count_mismatch"


@pytest.mark.parametrize(
    ("oldField", "replacement"),
    [(b",ir=1", b""), (b",rail=1", b",rail=unknown")],
)
def test_current_header_requires_all_production_diagnostics(oldField: bytes, replacement: bytes):
    packet = (FIXTURES / "volt_rows2_mixed.txt").read_bytes().replace(oldField, replacement, 1)
    events = MeasurementAsciiParser().feed(envelope(packet))
    assert not frames(events)
    assert errors(events)[0].reason == "bad_header"


def test_bad_x_token_rejects_and_next_crc_valid_frame_recovers():
    badValues = ["1", "X3", "3", "4", "5", "6", "7", "8"]
    badPacket = build_measurement_packet("V", 1, seq=110, values=badValues)
    goodPacket = build_measurement_packet("V", 1, seq=111)
    parser = MeasurementAsciiParser()
    events = parser.feed(envelope(badPacket + goodPacket))
    assert "bad_x" in [event.reason for event in errors(events)]
    parsedFrames = frames(events)
    assert len(parsedFrames) == 1
    assert parsedFrames[0].seq == 111


def test_bad_crc_frame_recovers_to_following_frame():
    badPacket = build_measurement_packet("R", 1, seq=120).replace(b"D0,-10", b"D0,-11", 1)
    goodPacket = build_measurement_packet("R", 1, seq=121)
    parser = MeasurementAsciiParser()
    events = parser.feed(envelope(badPacket + goodPacket))
    assert "crc" in [event.reason for event in errors(events)]
    assert [frame.seq for frame in frames(events)] == [121]


def test_registry_routes_current_packets_by_content_on_log_and_fragmented_ble():
    voltagePacket = (FIXTURES / "volt_rows2_mixed.txt").read_bytes()
    registry = ProtocolRegistry()
    logFrames = frames(registry.feed(envelope(voltagePacket, channel="log", source="ble")))
    assert len(logFrames) == 1 and logFrames[0].mode == "VOLT"

    resistancePacket = (FIXTURES / "res_rows1_mixed.txt").read_bytes()
    payloadCrc = crc32_reflected(resistancePacket)
    splitAt = len(resistancePacket) // 2
    fragment0 = (
        f"G,D,91,0,2,{splitAt},{len(resistancePacket)},{payloadCrc:08X}\n".encode("ascii")
        + resistancePacket[:splitAt]
    )
    fragment1 = (
        f"G,D,91,1,2,{len(resistancePacket) - splitAt},{len(resistancePacket)},{payloadCrc:08X}\n".encode("ascii")
        + resistancePacket[splitAt:]
    )
    assert not frames(registry.feed(envelope(fragment0, channel="log", source="ble")))
    bleFrames = frames(registry.feed(envelope(fragment1, channel="log", source="ble")))
    assert len(bleFrames) == 1 and bleFrames[0].mode == "RES"


def test_ble_log_notification_cannot_abort_partial_data_frame():
    packetLines = (FIXTURES / "volt_rows2_mixed.txt").read_bytes().splitlines(keepends=True)
    registry = ProtocolRegistry()
    assert not frames(registry.feed(envelope(b"".join(packetLines[:2]), channel="data", source="ble")))
    logEvents = registry.feed(
        envelope(
            b"ABAT,bt=4012,valid=1,fresh=1,ageMs=12,reason=ok\n",
            channel="log",
            source="ble",
        )
    )
    assert any(isinstance(event, BatteryTelemetry) for event in logEvents)
    completed = frames(registry.feed(envelope(b"".join(packetLines[2:]), channel="data", source="ble")))
    assert len(completed) == 1
    assert completed[0].mode == "VOLT"


def test_serial_log_line_between_chunks_does_not_abort_measurement_frame():
    packetLines = (FIXTURES / "volt_rows2_mixed.txt").read_bytes().splitlines(keepends=True)
    registry = ProtocolRegistry()
    assert not frames(registry.feed(envelope(b"".join(packetLines[:2]), source="serial")))
    logEvents = registry.feed(envelope(b"SF50,cfps=200,efps=50,ofps=5/50/50\n", source="serial"))
    assert any(getattr(event, "tag", "") == "SF50" for event in logEvents)
    completed = frames(registry.feed(envelope(b"".join(packetLines[2:]), source="serial")))
    assert len(completed) == 1


def test_independent_ble_channels_can_assemble_complete_measurement_packets_concurrently():
    voltageLines = (FIXTURES / "volt_rows2_mixed.txt").read_bytes().splitlines(keepends=True)
    resistancePacket = (FIXTURES / "res_rows1_mixed.txt").read_bytes()
    registry = ProtocolRegistry()
    assert not frames(registry.feed(envelope(b"".join(voltageLines[:2]), channel="data", source="ble")))
    resistanceFrames = frames(registry.feed(envelope(resistancePacket, channel="log", source="ble")))
    assert [frame.mode for frame in resistanceFrames] == ["RES"]
    voltageFrames = frames(registry.feed(envelope(b"".join(voltageLines[2:]), channel="data", source="ble")))
    assert [frame.mode for frame in voltageFrames] == ["VOLT"]


def test_registry_keeps_legacy_matv_support_separate_from_current_v_header():
    values = ",".join("0" for _ in range(64))
    registry = ProtocolRegistry()
    events = registry.feed(envelope(f"MATV,3,123,5,uV,{values}\n".encode("ascii")))
    legacyFrames = [event for event in events if isinstance(event, VoltageFrame)]
    assert len(legacyFrames) == 1
    assert legacyFrames[0].frameType == "MATV"


def test_unrelated_malformed_csv_diagnostic_is_not_a_legacy_matv_reject():
    registry = ProtocolRegistry()
    events = registry.feed(envelope(b'DIAG,detail="unterminated\n', source="serial"))
    assert not errors(events)
    assert any(getattr(event, "tag", "") == "DIAG" for event in events)


def test_malformed_legacy_matv_csv_remains_an_explicit_reject():
    registry = ProtocolRegistry()
    events = registry.feed(envelope(b'MATV,3,123,5,uV,"unterminated\n', source="serial"))
    assert any(event.reason == "matv_csv" for event in errors(events))
