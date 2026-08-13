from __future__ import annotations

import time
import re
import zlib
from dataclasses import replace

import numpy as np
import pytest

from sensorarray_app.domain.models import LogRecord, MixedMeasurementFrame, ParserErrorEvent, TransportEnvelope
from sensorarray_app.protocol.mixed_ascii import MixedMeasurementAsciiParser
from sensorarray_app.protocol.registry import ProtocolRegistry


PROFILE = "RVVCCVVR"


def envelope(payload: bytes, *, channel: str = "data", source: str = "replay") -> TransportEnvelope:
    return TransportEnvelope(
        source=source,
        channel=channel,
        deviceId="MIXED_TEST",
        sessionGeneration=3,
        receivedMonotonicNs=time.monotonic_ns(),
        receivedWallTime=1234.5,
        rawPayload=payload,
    )


def build_mixed_packet(
    *,
    rows: int = 5,
    seq: int = 71,
    profile: str = PROFILE,
    canonicalFirmwareWire: bool = True,
) -> bytes:
    modes = {
        "C": ("CAP", "pF", -6, "pf6"),
        "V": ("VOLT", "V", -6, "uv-x"),
        "R": ("RES", "ohm", -3, "mohm-x"),
    }
    identityFields = (
        "rgen=4,rrid=14,pgen=9,prid=42"
        if canonicalFirmwareWire
        else "rowsGen=4,rowsRid=14,profileGen=9,profileRid=42"
    )
    lines = [
        f"M,seq={seq},ts=998877,rows={rows},cells={rows * 8},{identityFields},"
        f"profile={profile},fmt=mix1"
        + ("" if canonicalFirmwareWire else f",n={rows * 8}")
    ]
    for row in range(1, rows + 1):
        mode, unit, scale, rowFormat = modes[profile[row - 1]]
        if mode == "CAP":
            # CAP pf6 represents total capacitance; the host applies its
            # configured circuit offset only after preserving these integers.
            values = [str(39_000_000 + row * 100_000 + cell * 1_000) for cell in range(8)]
            pga = ""
        elif mode == "VOLT":
            values = [str(-1_000_000 + row * 100_000 + cell * 10_000) for cell in range(8)]
            pga = ",pga=0001020408102000"
        else:
            values = [str(10_025_000 + row * 1_000 + cell * 10) for cell in range(8)]
            pga = ",pga=0001020408102000"
        if canonicalFirmwareWire:
            lines.append(
                f"MR,s={row},m={profile[row - 1]},unit={unit},scale={scale},valid=FF,fresh=FF,error=00,"
                f"fmt={rowFormat},D={','.join(values)}"
            )
        else:
            lines.append(
                f"MR,row={row},mode={mode},unit={unit},scale={scale},valid=FF,fresh=FF,error=00,"
                f"ref={'FDC' if mode == 'CAP' else 'INTREF'},rail={0 if mode == 'CAP' else 1},age=1,"
                f"values={'|'.join(values)}{pga}"
            )
    crcPayload = "".join(line + "\n" for line in lines).encode("ascii")
    lines.append(
        f"K,seq={seq},{identityFields},"
        f"crc={zlib.crc32(crcPayload) & 0xFFFFFFFF:08X}"
    )
    return "".join(line + "\n" for line in lines).encode("ascii")


def frames(events: list[object]) -> list[MixedMeasurementFrame]:
    return [event for event in events if isinstance(event, MixedMeasurementFrame)]


def errors(events: list[object]) -> list[ParserErrorEvent]:
    return [event for event in events if isinstance(event, ParserErrorEvent)]


def rebuild_crc(lines: list[bytes]) -> bytes:
    headerFields = dict(item.split("=", 1) for item in lines[0].decode().strip().split(",")[1:])
    crcPayload = b"".join(line.rstrip(b"\r\n") + b"\n" for line in lines[:-1])
    identityKeys = (
        ("rgen", "rrid", "pgen", "prid")
        if "rgen" in headerFields
        else ("rowsGen", "rowsRid", "profileGen", "profileRid")
    )
    trailer = (
        f"K,seq={headerFields['seq']},"
        + ",".join(f"{key}={headerFields[key]}" for key in identityKeys)
        + ","
        f"crc={zlib.crc32(crcPayload) & 0xFFFFFFFF:08X}\n"
    ).encode("ascii")
    return crcPayload + trailer


def test_mixed_frame_is_atomic_and_preserves_physical_row_identity_and_units():
    packet = build_mixed_packet()
    lines = packet.splitlines(keepends=True)
    parser = MixedMeasurementAsciiParser()
    assert not frames(parser.feed(envelope(b"".join(lines[:-1]))))
    parsedFrames = frames(parser.feed(envelope(lines[-1])))
    assert len(parsedFrames) == 1
    frame = parsedFrames[0]
    assert (frame.seq, frame.rows, frame.cells) == (71, 5, 40)
    assert (frame.rowsGeneration, frame.rowsRequestId) == (4, 14)
    assert (frame.profileGeneration, frame.profileRequestId) == (9, 42)
    assert frame.profile == ("RES", "VOLT", "VOLT", "CAP", "CAP", "VOLT", "VOLT", "RES")
    assert [row.row for row in frame.rowFrames] == [1, 2, 3, 4, 5]
    assert [row.mode for row in frame.rowFrames] == ["RES", "VOLT", "VOLT", "CAP", "CAP"]
    assert [row.unit for row in frame.rowFrames] == ["ohm", "V", "V", "pF", "pF"]
    assert frame.rowFrames[0].physicalValues[0] == pytest.approx(10_026.0)
    assert frame.rowFrames[1].physicalValues[0] == pytest.approx(-0.8)
    assert frame.rowFrames[3].physicalValues[0] == pytest.approx(6.4)
    # Firmware 331c445's MR formatter does not emit per-row PGA metadata.
    assert frame.rowFrames[0].pgaBypassMask is None
    assert frame.rowFrames[3].pgaValues is None


@pytest.mark.parametrize("rows", range(1, 9))
def test_mixed_parser_accepts_every_geometry_when_saved_profile_is_heterogeneous(rows: int):
    frame = frames(MixedMeasurementAsciiParser().feed(envelope(build_mixed_packet(rows=rows))))[0]
    assert frame.rows == rows
    assert frame.cells == rows * 8
    assert [rowFrame.row for rowFrame in frame.rowFrames] == list(range(1, rows + 1))


@pytest.mark.parametrize(
    ("rows", "profile"),
    [
        (1, "CCCCCCCC"),
        (4, "VVVVVVVV"),
        (8, "RRRRRRRR"),
    ],
)
def test_mixed_parser_rejects_fully_homogeneous_profile_reserved_for_legacy_frames(rows: int, profile: str):
    events = MixedMeasurementAsciiParser().feed(envelope(build_mixed_packet(rows=rows, profile=profile)))
    assert not frames(events)
    assert "homogeneous_profile" in [event.reason for event in errors(events)]


def test_typed_mixed_frame_cannot_bypass_heterogeneous_saved_profile_invariant():
    frame = frames(MixedMeasurementAsciiParser().feed(envelope(build_mixed_packet(rows=2))))[0]
    with pytest.raises(ValueError, match="heterogeneous saved row profile"):
        replace(frame, profile=("RES",) * 8)


def test_mixed_crc_covers_m_and_all_mr_lines_and_bad_crc_never_emits():
    packet = build_mixed_packet(rows=2)
    corrupted = packet.replace(b"D=10026000", b"D=10026001", 1)
    parser = MixedMeasurementAsciiParser()
    events = parser.feed(envelope(corrupted))
    assert not frames(events)
    assert [event.reason for event in errors(events)] == ["crc"]
    assert parser.stats.crcFailures == 1


def test_incomplete_mixed_frame_is_rejected_at_k_without_store_visible_frame():
    lines = build_mixed_packet(rows=3).splitlines(keepends=True)
    events = MixedMeasurementAsciiParser().feed(envelope(b"".join(lines[:-2] + lines[-1:])))
    assert not frames(events)
    assert "missing_rows" in [event.reason for event in errors(events)]


def test_duplicate_row_is_rejected_atomically_and_parser_recovers_next_frame():
    badLines = build_mixed_packet(rows=2, seq=80).splitlines(keepends=True)
    badPacket = b"".join(badLines[:2] + [badLines[1]] + badLines[2:])
    parser = MixedMeasurementAsciiParser()
    events = parser.feed(envelope(badPacket + build_mixed_packet(rows=2, seq=81)))
    assert "duplicate_row" in [event.reason for event in errors(events)]
    assert [frame.seq for frame in frames(events)] == [81]


def test_profile_mismatch_is_rejected_before_quantity_can_pollute_a_domain():
    lines = build_mixed_packet(rows=2).splitlines(keepends=True)
    lines[1] = lines[1].replace(b"m=R", b"m=C")
    events = MixedMeasurementAsciiParser().feed(envelope(rebuild_crc(lines)))
    assert not frames(events)
    assert "profile_mismatch" in [event.reason for event in errors(events)]


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ((b"valid=FF", b"valid=FE"), "valid_token_mismatch"),
        ((b"error=00", b"error=01"), "error_token_mismatch"),
        ((b"fmt=mohm-x", b"fmt=uv-x"), "row_format_mismatch"),
        ((b"unit=ohm", b"unit=pF"), "row_quantity_mismatch"),
    ],
)
def test_mixed_row_mask_pga_and_quantity_consistency(mutation: tuple[bytes, bytes], reason: str):
    lines = build_mixed_packet(rows=2).splitlines(keepends=True)
    lines[1] = lines[1].replace(*mutation, 1)
    events = MixedMeasurementAsciiParser().feed(envelope(rebuild_crc(lines)))
    assert not frames(events)
    assert reason in [event.reason for event in errors(events)]


def test_voltage_and_resistance_rows_accept_omitted_optional_pga_metadata():
    lines = build_mixed_packet(rows=2, canonicalFirmwareWire=False).splitlines(keepends=True)
    lines[1] = lines[1].replace(b",pga=0001020408102000", b"")
    lines[2] = lines[2].replace(b",pga=0001020408102000", b"")
    frame = frames(MixedMeasurementAsciiParser().feed(envelope(rebuild_crc(lines))))[0]
    assert [rowFrame.mode for rowFrame in frame.rowFrames] == ["RES", "VOLT"]
    assert all(rowFrame.pgaValues is None for rowFrame in frame.rowFrames)
    assert all(rowFrame.pgaBypassMask is None for rowFrame in frame.rowFrames)


def test_saved_long_field_replay_alias_still_accepts_optional_pga_metadata():
    frame = frames(
        MixedMeasurementAsciiParser().feed(
            envelope(build_mixed_packet(rows=2, canonicalFirmwareWire=False))
        )
    )[0]
    assert frame.rowFrames[0].pgaValues is not None
    assert frame.rowFrames[0].pgaBypassMask is not None


def test_registry_routes_fragmented_mixed_stream_without_affecting_legacy_routes():
    packet = build_mixed_packet(rows=5)
    splitAt = len(packet) // 2
    registry = ProtocolRegistry()
    assert not frames(registry.feed(envelope(packet[:splitAt], channel="log", source="ble")))
    parsedFrames = frames(registry.feed(envelope(packet[splitAt:], channel="log", source="ble")))
    assert len(parsedFrames) == 1
    assert parsedFrames[0].sourceTransport == "ble"
    assert [row.row for row in parsedFrames[0].rowFrames] == [1, 2, 3, 4, 5]


def test_firmware_ble_m_stage_diagnostic_is_not_misparsed_as_mixed_header():
    registry = ProtocolRegistry()
    events = registry.feed(
        envelope(
            b"M,stage=ble_alloc,reason=start,ih=1,il=2,im=3\n",
            channel="log",
            source="ble",
        )
    )
    assert not errors(events)
    assert any(isinstance(event, LogRecord) and event.tag == "M" for event in events)
    assert not registry.mixed.hasPendingFrame


def test_mixed_header_profile_must_always_store_all_eight_rows():
    badPacket = build_mixed_packet(rows=2).replace(b"profile=RVVCCVVR", b"profile=RV")
    events = MixedMeasurementAsciiParser().feed(envelope(badPacket))
    assert not frames(events)
    assert errors(events)[0].reason == "bad_m_header"


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ((b",fmt=mix1", b""), "header_format_mismatch"),
        ((b",fmt=mix1", b",fmt=mix2"), "header_format_mismatch"),
        ((b",fmt=mohm-x,D=", b",D="), "row_format_mismatch"),
        ((b",fmt=mohm-x,D=", b",fmt=uv-x,D="), "row_format_mismatch"),
    ],
)
def test_canonical_firmware_mixed_format_markers_are_required(
    mutation: tuple[bytes, bytes],
    reason: str,
):
    lines = build_mixed_packet(rows=2).splitlines(keepends=True)
    target = 0 if b"mix1" in mutation[0] else 1
    lines[target] = lines[target].replace(*mutation, 1)
    events = MixedMeasurementAsciiParser().feed(envelope(rebuild_crc(lines)))
    assert not frames(events)
    assert reason in [event.reason for event in errors(events)]


@pytest.mark.parametrize("mask", [b"F", b"0FF", b"0xFF"])
def test_canonical_firmware_mixed_masks_are_exactly_two_hex_characters(mask: bytes):
    lines = build_mixed_packet(rows=2).splitlines(keepends=True)
    lines[1] = lines[1].replace(b"valid=FF", b"valid=" + mask, 1)
    events = MixedMeasurementAsciiParser().feed(envelope(rebuild_crc(lines)))
    assert not frames(events)
    assert "bad_row_mask_width" in [event.reason for event in errors(events)]


@pytest.mark.parametrize(
    "mutation",
    [
        (b"MR,s=1,m=R", b"MR,row=1,m=R"),
        (b"MR,s=1,m=R", b"MR,s=1,mode=RES"),
        (b",D=", b",values="),
    ],
)
def test_canonical_firmware_mixed_row_requires_short_schema(mutation: tuple[bytes, bytes]):
    lines = build_mixed_packet(rows=2).splitlines(keepends=True)
    lines[1] = lines[1].replace(*mutation, 1)
    events = MixedMeasurementAsciiParser().feed(envelope(rebuild_crc(lines)))
    assert not frames(events)
    assert "mixed_row_schema" in [event.reason for event in errors(events)]


def test_canonical_firmware_mixed_trailer_requires_short_identity_schema():
    lines = build_mixed_packet(rows=2).splitlines(keepends=True)
    lines[-1] = lines[-1].replace(b"rgen=4,rrid=14,pgen=9,prid=42", b"rowsGen=4,rowsRid=14,profileGen=9,profileRid=42")
    events = MixedMeasurementAsciiParser().feed(envelope(b"".join(lines)))
    assert not frames(events)
    assert "mixed_trailer_schema" in [event.reason for event in errors(events)]


@pytest.mark.parametrize("crc", [b"ABCDEF0", b"0ABCDEF01", b"0xABCDEF"])
def test_canonical_firmware_mixed_crc_is_exactly_eight_hex_characters(crc: bytes):
    lines = build_mixed_packet(rows=2).splitlines(keepends=True)
    lines[-1] = re.sub(rb"crc=[0-9A-F]{8}", b"crc=" + crc, lines[-1])
    events = MixedMeasurementAsciiParser().feed(envelope(b"".join(lines)))
    assert not frames(events)
    assert "bad_crc_width" in [event.reason for event in errors(events)]


def test_xhh_value_retains_error_code_and_never_becomes_numeric():
    lines = build_mixed_packet(rows=2).splitlines(keepends=True)
    lines[1] = lines[1].replace(b"valid=FF", b"valid=FE").replace(b"error=00", b"error=01")
    firstValue = lines[1].split(b"D=", 1)[1].split(b",", 1)[0]
    lines[1] = lines[1].replace(b"D=" + firstValue, b"D=X0D", 1)
    frame = frames(MixedMeasurementAsciiParser().feed(envelope(rebuild_crc(lines))))[0]
    assert np.isnan(frame.rowFrames[0].physicalValues[0])
    assert frame.rowFrames[0].errorCodes[0] == 0x0D
    assert frame.rowFrames[0].errorReasons[0] == "Open circuit"
