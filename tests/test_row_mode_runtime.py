from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from sensorarray_app.app.configuration import AppConfiguration
from sensorarray_app.domain.battery import parse_battery_fields
from sensorarray_app.domain.models import (
    CapacitanceFrame,
    CommandTransactionEvent,
    DisplayMode,
    MeasurementFrame,
    MixedMeasurementFrame,
    RailTelemetry,
    RowMeasurement,
    TransportEnvelope,
)
from sensorarray_app.protocol.registry import ProtocolRegistry
from sensorarray_app.services.command_service import CommandService
from sensorarray_app.store.history_store import MatrixHistoryStore
from sensorarray_app.store.matrix_store import MatrixStore
from sensorarray_app.store.telemetry_store import TelemetryStore
from sensorarray_backend.app import create_app
from sensorarray_backend.core.runtime import BackendRuntime
from sensorarray_backend.core.snapshot import _display_matrix, snapshot_payload


PROFILE = ("RES", "VOLT", "VOLT", "CAP", "CAP", "VOLT", "VOLT", "RES")


def command_event(
    phase: str,
    request_id: int | None,
    *,
    requested=None,
    applied=None,
    generation: int | None = None,
    seq: int | None = None,
    error: str | None = None,
) -> CommandTransactionEvent:
    return CommandTransactionEvent(
        commandType="row_modes",
        phase=phase,
        requestId=request_id,
        requestedValue=requested,
        appliedValue=applied,
        generation=generation,
        frameSeq=seq,
        error=error,
        sessionGeneration=0,
    )


def row_measurement(row: int, mode: str, value: float) -> RowMeasurement:
    unit, scale, multiplier = {
        "CAP": ("pF", -6, 1_000_000.0),
        "VOLT": ("V", -6, 1_000_000.0),
        "RES": ("ohm", -3, 1_000.0),
    }[mode]
    physical = np.full(8, value, dtype=np.float64)
    raw_fixed = np.rint(physical * multiplier).astype(np.float64)
    valid = np.ones(8, dtype=bool)
    errors = np.zeros(8, dtype=bool)
    error_codes = np.zeros(8, dtype=np.uint8)
    pga = None if mode == "CAP" else np.ones(8, dtype=np.uint8)
    bypass = None if mode == "CAP" else np.zeros(8, dtype=bool)
    return RowMeasurement(
        row=row,
        mode=mode,
        unit=unit,
        scale=scale,
        rawFixedValues=raw_fixed,
        physicalValues=physical,
        validMask=valid,
        freshMask=valid.copy(),
        errorMask=errors,
        errorCodes=error_codes,
        errorReasons=("",) * 8,
        pgaValues=pga,
        pgaBypassMask=bypass,
        reference=None,
        railValid=True if mode != "CAP" else None,
        railAgeFrames=0 if mode != "CAP" else None,
        expectedMask=np.ones(8, dtype=bool),
        acquiredMask=np.ones(8, dtype=bool),
    )


def mixed_frame(seq: int, profile=PROFILE, cap_value: float = 6.3) -> MixedMeasurementFrame:
    profile = tuple(profile)
    row_frames = []
    for row, mode in enumerate(profile, start=1):
        value = cap_value if mode == "CAP" else (0.1 * row if mode == "VOLT" else 10_000.0 + row)
        row_frames.append(row_measurement(row, mode, value))
    return MixedMeasurementFrame(
        seq=seq,
        timestampUs=seq * 1_000,
        rows=8,
        cells=64,
        rowsGeneration=2,
        rowsRequestId=3,
        profileGeneration=11,
        profileRequestId=62,
        profile=profile,
        rowFrames=tuple(row_frames),
        sourceTransport="serial",
        sessionGeneration=0,
        receivedTime=time.time(),
        receivedMonotonicNs=time.monotonic_ns(),
        expectedMask=np.ones(64, dtype=bool),
        acquiredMask=np.ones(64, dtype=bool),
    )


def test_row_profile_transaction_is_atomic_strict_and_times_out_without_fabricating_apply():
    service = CommandService()
    sent: list[str] = []
    service.request_row_modes(PROFILE, sent.append)
    assert sent == ["ROWMODES=RVVCCVVR"]
    assert service.appliedRowModes == ("CAP",) * 8
    assert service.pendingRowModes == PROFILE

    service.handle(command_event("accepted", 62, requested=PROFILE))
    assert service.rowModeTransitionState == "accepted"
    service.handle(command_event("accepted", 63, requested=PROFILE))
    assert service.rowModeRequestId == 62
    assert service.snapshot()["latestCommand"]["firmwareId"] == 62
    service.handle(command_event("applied", 99, applied=PROFILE, generation=11, seq=201))
    assert service.appliedRowModes == ("CAP",) * 8
    assert service.pendingRowModes == PROFILE

    wrong_profile = ("CAP", "RES", "VOLT", "CAP", "RES", "VOLT", "CAP", "RES")
    rejected = service.handle(command_event("applied", 62, applied=wrong_profile, generation=11, seq=201))
    assert rejected["rejectedMismatchedValue"] is True
    assert service.appliedRowModes == ("CAP",) * 8
    assert service.pendingRowModes == PROFILE
    assert service.snapshot()["latestCommand"]["state"] == "ACCEPTED"

    result = service.handle(command_event("applied", 62, applied=PROFILE, generation=11, seq=201))
    assert result["rowModesApplied"] is True
    assert service.appliedRowModes == PROFILE
    assert service.pendingRowModes is None
    assert (service.rowModeRequestId, service.rowModeGeneration, service.rowModeFrameSeq) == (62, 11, 201)

    next_profile = ("CAP", "RES", "VOLT", "CAP", "RES", "VOLT", "CAP", "RES")
    service.request_row_modes(next_profile, lambda _command: None)
    service.timeout_old(seconds=-1.0)
    assert service.rowModeTransitionState == "outcome_unknown"
    assert service.appliedRowModes == PROFILE
    assert service.pendingRowModes == next_profile
    assert "RMAPP" in service.rowModeError


def test_row_profile_error_requires_matching_non_null_firmware_id_after_rmack():
    service = CommandService()
    service.request_row_modes(PROFILE, lambda _command: None)
    service.handle(command_event("accepted", 62, requested=PROFILE))

    service.handle(command_event("failed", None, requested=PROFILE, error="missing_id"))
    service.handle(command_event("failed", 99, requested=PROFILE, error="wrong_id"))
    wrong_profile = ("CAP", "RES", "VOLT", "CAP", "RES", "VOLT", "CAP", "RES")
    rejected = service.handle(command_event("failed", 62, requested=wrong_profile, error="wrong_profile"))
    assert rejected["rejectedMismatchedValue"] is True
    assert service.pendingRowModes == PROFILE
    assert service.rowModeTransitionState == "accepted"

    service.handle(command_event("failed", 62, requested=PROFILE, error="route"))
    assert service.pendingRowModes is None
    assert service.rowModeTransitionState == "error"
    assert service.rowModeError == "route"


def test_mode_and_row_profile_transactions_reject_missing_request_ids():
    service = CommandService()
    service.request_mode("VOLT", lambda _command: None)
    missing_mode_ack = CommandTransactionEvent(
        commandType="mode",
        phase="accepted",
        requestId=None,
        requestedValue="VOLT",
    )
    missing_mode_app = CommandTransactionEvent(
        commandType="mode",
        phase="applied",
        requestId=None,
        requestedValue="VOLT",
        appliedValue="VOLT",
        generation=7,
        frameSeq=8,
    )
    assert service.handle(missing_mode_ack)["rejectedMissingRequestId"] is True
    assert service.handle(missing_mode_app)["rejectedMissingRequestId"] is True
    assert service.appliedMode == "CAP"
    assert service.pendingMode == "VOLT"
    assert service.transitionState == "requested"

    service = CommandService()
    service.request_row_modes(PROFILE, lambda _command: None)
    missing_row_ack = command_event("accepted", None, requested=PROFILE)
    missing_row_app = command_event("applied", None, applied=PROFILE, generation=11, seq=201)
    assert service.handle(missing_row_ack)["rejectedMissingRequestId"] is True
    assert service.handle(missing_row_app)["rejectedMissingRequestId"] is True
    assert service.appliedRowModes == ("CAP",) * 8
    assert service.pendingRowModes == PROFILE
    assert service.rowModeTransitionState == "requested"


@pytest.mark.parametrize("command_type", ["mode", "row_modes"])
@pytest.mark.parametrize(("generation", "seq"), [(None, 8), (7, None), (-1, 8), (7, -1)])
def test_applied_mode_transactions_require_complete_nonnegative_boundary(
    command_type: str,
    generation: int | None,
    seq: int | None,
):
    service = CommandService()
    if command_type == "mode":
        service.request_mode("VOLT", lambda _command: None)
        service.handle(
            CommandTransactionEvent(
                commandType="mode",
                phase="accepted",
                requestId=42,
                requestedValue="VOLT",
            )
        )
        event = CommandTransactionEvent(
            commandType="mode",
            phase="applied",
            requestId=42,
            requestedValue="VOLT",
            appliedValue="VOLT",
            generation=generation,
            frameSeq=seq,
        )
    else:
        service.request_row_modes(PROFILE, lambda _command: None)
        service.handle(command_event("accepted", 62, requested=PROFILE))
        event = command_event("applied", 62, applied=PROFILE, generation=generation, seq=seq)

    assert service.handle(event)["rejectedMissingBoundary"] is True
    if command_type == "mode":
        assert service.pendingMode == "VOLT"
        assert service.appliedMode == "CAP"
    else:
        assert service.pendingRowModes == PROFILE
        assert service.appliedRowModes == ("CAP",) * 8


def test_mode_and_row_profile_requests_are_mutually_exclusive_while_pending():
    service = CommandService()
    service.request_mode("VOLT", lambda _command: None)
    with pytest.raises(RuntimeError, match="measurement mode transaction"):
        service.request_mode("RES", lambda _command: None)
    with pytest.raises(RuntimeError, match="measurement mode transaction"):
        service.request_row_modes(PROFILE, lambda _command: None)

    other = CommandService()
    other.request_row_modes(PROFILE, lambda _command: None)
    with pytest.raises(RuntimeError, match="row-mode profile transaction"):
        other.request_row_modes(("RES",) * 8, lambda _command: None)
    with pytest.raises(RuntimeError, match="row-mode profile transaction"):
        other.request_mode("VOLT", lambda _command: None)


def test_global_mapp_sets_all_rows_without_forging_independent_profile_generation():
    service = CommandService()
    service.request_mode("RES", lambda _command: None)
    service.handle(
        CommandTransactionEvent(
            commandType="mode",
            phase="accepted",
            requestId=42,
            requestedValue="RES",
        )
    )
    result = service.handle(
        CommandTransactionEvent(
            commandType="mode",
            phase="applied",
            requestId=42,
            requestedValue="RES",
            appliedValue="RES",
            generation=9,
            frameSeq=301,
        )
    )
    assert result["modeApplied"] is True
    assert service.appliedRowModes == ("RES",) * 8
    assert (service.rowModeRequestId, service.rowModeGeneration, service.rowModeFrameSeq) == (42, None, 301)
    assert service.rowModeTransitionState == "applied"
    assert service.rowModeError == ""


def test_remote_global_mapp_cannot_complete_or_steal_pending_row_profile_identity():
    service = CommandService()
    service.request_row_modes(PROFILE, lambda _command: None)
    service.handle(command_event("accepted", 62, requested=PROFILE))

    # Model a transaction initiated by another host/controller. It can change
    # the device's applied all-row profile, but it is not the pending RMAPP.
    service.pendingMode = "VOLT"
    service.modeRequestId = 71
    service.transitionState = "accepted"
    result = service.handle(
        CommandTransactionEvent(
            commandType="mode",
            phase="applied",
            requestId=71,
            requestedValue="VOLT",
            appliedValue="VOLT",
            generation=12,
            frameSeq=400,
        )
    )
    assert result["modeApplied"] is True
    assert service.appliedRowModes == ("VOLT",) * 8
    assert service.pendingRowModes == PROFILE
    assert service.rowModeRequestId == 62
    assert service.rowModeTransitionState == "accepted"


def test_mode_and_row_profile_generations_remain_independent_across_mapp_then_rmapp():
    fixture = Path(__file__).parent / "fixtures" / "current_protocol" / "volt_rows2_mixed.txt"
    envelope = TransportEnvelope(
        source="replay",
        channel="data",
        deviceId="DIVERGED_GENERATIONS",
        sessionGeneration=0,
        receivedMonotonicNs=time.monotonic_ns(),
        receivedWallTime=time.time(),
        rawPayload=fixture.read_bytes(),
    )
    voltage_frame = next(event for event in ProtocolRegistry().feed(envelope) if isinstance(event, MeasurementFrame))
    runtime = BackendRuntime(AppConfiguration())
    runtime.commands.request_mode("VOLT", lambda _command: None)
    runtime._handle_event(
        CommandTransactionEvent(
            commandType="mode",
            phase="accepted",
            requestId=voltage_frame.requestId,
            requestedValue="VOLT",
        )
    )
    runtime._handle_event(
        CommandTransactionEvent(
            commandType="mode",
            phase="applied",
            requestId=voltage_frame.requestId,
            requestedValue="VOLT",
            appliedValue="VOLT",
            generation=voltage_frame.generation,
            frameSeq=voltage_frame.seq,
        )
    )
    runtime._handle_event(voltage_frame)
    after_mode = snapshot_payload(runtime)
    assert after_mode["measurement"]["generation"] == voltage_frame.generation
    assert after_mode["measurement"]["rowProfile"]["generation"] is None
    assert after_mode["frame"]["profileGeneration"] is None
    assert after_mode["frame"]["profileRequestId"] == voltage_frame.requestId

    runtime.commands.request_row_modes(PROFILE, lambda _command: None)
    runtime._handle_event(command_event("accepted", 62, requested=PROFILE))
    runtime._handle_event(command_event("applied", 62, applied=PROFILE, generation=17, seq=400))
    authoritative_mixed = replace(
        mixed_frame(401),
        profileGeneration=17,
        profileRequestId=62,
    )
    runtime._handle_event(authoritative_mixed)
    after_row_modes = snapshot_payload(runtime)
    assert after_row_modes["measurement"]["generation"] == voltage_frame.generation
    assert after_row_modes["measurement"]["rowProfile"]["generation"] == 17
    assert after_row_modes["frame"]["profileGeneration"] == 17


def test_homogeneous_row_profile_rmapp_arms_all_legacy_frame_gates():
    fixture_root = Path(__file__).parent / "fixtures" / "current_protocol"

    def parse_measurement(name: str):
        envelope = TransportEnvelope(
            source="replay",
            channel="data",
            deviceId="ROW_PROFILE_GATE",
            sessionGeneration=0,
            receivedMonotonicNs=time.monotonic_ns(),
            receivedWallTime=time.time(),
            rawPayload=(fixture_root / name).read_bytes(),
        )
        return next(event for event in ProtocolRegistry().feed(envelope) if isinstance(event, MeasurementFrame))

    voltage_frame = parse_measurement("volt_rows2_mixed.txt")
    resistance_frame = parse_measurement("res_rows1_mixed.txt")
    cap_frame = CapacitanceFrame(
        seq=20,
        timestampUs=20_000,
        rows=1,
        cells=8,
        generation=99,
        requestId=88,
        rowFreshMask=1,
        primaryFreshMask=1,
        secondaryFreshMask=1,
        badStaleCount=0,
        badMixedCount=0,
        badInvalidCount=0,
        rawFixedValues=np.full(8, 40_000_000, dtype=np.int64),
        rawPfValues=np.full(8, 40.0),
        correctedPfValues=np.full(8, 7.0),
        validMask=np.ones(8, dtype=bool),
        sourceTransport="replay",
        sessionGeneration=0,
        receivedTime=time.time(),
        receivedMonotonicNs=time.monotonic_ns(),
    )

    cases = (
        ("CAP", cap_frame, 20, 11, 62),
        ("VOLT", voltage_frame, voltage_frame.seq, voltage_frame.generation, voltage_frame.requestId),
        ("RES", resistance_frame, resistance_frame.seq, resistance_frame.generation, resistance_frame.requestId),
    )
    for mode, frame, boundary, generation, request_id in cases:
        store = MatrixStore()
        prior_mode = "RES" if mode != "RES" else "VOLT"
        store.apply_measurement_mode(prior_mode, 1, 1, 1)
        accepted_before = store.add_capacitance(frame) if mode == "CAP" else store.add_measurement(frame)
        assert accepted_before is False

        store.apply_row_modes((mode,) * 8, generation, request_id, boundary)
        accepted_after = store.add_capacitance(frame) if mode == "CAP" else store.add_measurement(frame)
        assert accepted_after is True
        assert store.snapshot().mode == mode


def test_heterogeneous_saved_profile_uses_mixed_even_when_active_prefix_is_cap():
    profile = ("CAP", "CAP", "CAP", "CAP", "RES", "VOLT", "VOLT", "RES")
    store = MatrixStore()
    store.apply_rows(4, generation=3, request_id=14, frame_seq=19)
    store.apply_row_modes(profile, generation=11, request_id=62, frame_seq=20)
    legacy_frame = CapacitanceFrame(
        seq=20,
        timestampUs=20_000,
        rows=4,
        cells=32,
        generation=3,
        requestId=14,
        rowFreshMask=0x0F,
        primaryFreshMask=0x0F,
        secondaryFreshMask=0x0F,
        badStaleCount=0,
        badMixedCount=0,
        badInvalidCount=0,
        rawFixedValues=np.full(32, 40_000_000, dtype=np.int64),
        rawPfValues=np.full(32, 40.0),
        correctedPfValues=np.full(32, 7.0),
        validMask=np.ones(32, dtype=bool),
        sourceTransport="replay",
        sessionGeneration=0,
        receivedTime=time.time(),
        receivedMonotonicNs=time.monotonic_ns(),
    )

    # Firmware 8045 decides its frame family from all eight persisted row
    # modes, while the actual M wire profile uses N for inactive S5..S8.
    assert store.add_capacitance(legacy_frame) is False
    frame = replace(
        mixed_frame(20, profile=profile),
        rows=4,
        cells=32,
        rowsGeneration=3,
        rowsRequestId=14,
        profile=("CAP", "CAP", "CAP", "CAP", "NONE", "NONE", "NONE", "NONE"),
        rowFrames=tuple(row_measurement(row, "CAP", 7.0) for row in range(1, 5)),
        expectedMask=np.ones(32, dtype=bool),
        acquiredMask=np.ones(32, dtype=bool),
    )
    assert store.add_mixed(frame) is True
    snapshot = store.snapshot()
    assert snapshot.layout == "MIXED"
    assert snapshot.mode == "MIXED"
    assert snapshot.activeRows == 4
    assert snapshot.rowModes == profile
    assert snapshot.profileGeneration == 11
    assert snapshot.profileRequestId == 62


def test_rows_rapp_does_not_replace_independent_measurement_mode_gate():
    fixture = Path(__file__).parent / "fixtures" / "current_protocol" / "volt_rows2_mixed.txt"
    envelope = TransportEnvelope(
        source="replay",
        channel="data",
        deviceId="ROWS_GATE_INDEPENDENCE",
        sessionGeneration=0,
        receivedMonotonicNs=time.monotonic_ns(),
        receivedWallTime=time.time(),
        rawPayload=fixture.read_bytes(),
    )
    voltage_frame = next(
        event for event in ProtocolRegistry().feed(envelope)
        if isinstance(event, MeasurementFrame)
    )

    store = MatrixStore()
    store.apply_measurement_mode(
        "VOLT",
        generation=voltage_frame.generation,
        request_id=voltage_frame.requestId,
        frame_seq=voltage_frame.seq,
    )
    # A later ROWS transaction changes only geometry identity.  It must not
    # weaken or replace the already-applied MODE generation/request gate.
    store.apply_rows(2, generation=99, request_id=98, frame_seq=voltage_frame.seq + 1)

    current = replace(voltage_frame, seq=voltage_frame.seq + 2)
    assert store.add_measurement(current) is True
    stale_mode_generation = replace(
        voltage_frame,
        seq=voltage_frame.seq + 3,
        generation=voltage_frame.generation + 1,
    )
    assert store.add_measurement(stale_mode_generation) is False


def test_mixed_store_rejects_wrong_rows_geometry_generation_and_request_identity():
    store = MatrixStore()
    store.apply_rows(8, generation=2, request_id=3, frame_seq=100)
    store.apply_row_modes(PROFILE, generation=11, request_id=62, frame_seq=100)
    frame = mixed_frame(101)
    assert store.add_mixed(frame) is True

    store.apply_rows(5, generation=4, request_id=14, frame_seq=102)
    five_row_profile = ("RES", "VOLT", "VOLT", "CAP", "CAP", "VOLT", "VOLT", "RES")
    store.apply_row_modes(five_row_profile, generation=12, request_id=63, frame_seq=102)
    five_rows = replace(
        mixed_frame(102, profile=five_row_profile),
        rows=5,
        cells=40,
        rowsGeneration=4,
        rowsRequestId=14,
        profileGeneration=12,
        profileRequestId=63,
        profile=("RES", "VOLT", "VOLT", "CAP", "CAP", "NONE", "NONE", "NONE"),
        rowFrames=mixed_frame(102, profile=five_row_profile).rowFrames[:5],
        expectedMask=np.ones(40, dtype=bool),
        acquiredMask=np.ones(40, dtype=bool),
    )
    wrong_geometry = replace(
        five_rows,
        rows=4,
        cells=32,
        profile=("RES", "VOLT", "VOLT", "CAP", "NONE", "NONE", "NONE", "NONE"),
        rowFrames=five_rows.rowFrames[:4],
        expectedMask=np.ones(32, dtype=bool),
        acquiredMask=np.ones(32, dtype=bool),
    )
    assert store.add_mixed(wrong_geometry) is False
    assert store.add_mixed(replace(five_rows, rowsGeneration=5)) is False
    assert store.add_mixed(replace(five_rows, rowsRequestId=99)) is False
    assert store.add_mixed(five_rows) is True


def test_pending_row_profile_data_frame_neither_commits_nor_bypasses_rmapp():
    fixture = Path(__file__).parent / "fixtures" / "current_protocol" / "volt_rows2_mixed.txt"
    envelope = TransportEnvelope(
        source="replay",
        channel="data",
        deviceId="ROW_PROFILE_PENDING",
        sessionGeneration=0,
        receivedMonotonicNs=time.monotonic_ns(),
        receivedWallTime=time.time(),
        rawPayload=fixture.read_bytes(),
    )
    voltage_frame = next(
        event for event in ProtocolRegistry().feed(envelope)
        if isinstance(event, MeasurementFrame)
    )
    runtime = BackendRuntime(AppConfiguration())
    runtime.commands.request_row_modes(("VOLT",) * 8, lambda _command: None)
    runtime._handle_event(command_event("accepted", voltage_frame.requestId, requested=("VOLT",) * 8))

    runtime._handle_event(voltage_frame)
    assert runtime.commands.pendingRowModes == ("VOLT",) * 8
    assert runtime.commands.appliedRowModes == ("CAP",) * 8
    assert runtime.matrixStore.snapshot().seq is None

    runtime._handle_event(
        command_event(
            "applied",
            voltage_frame.requestId,
            applied=("VOLT",) * 8,
            generation=voltage_frame.generation,
            seq=voltage_frame.seq,
        )
    )
    runtime._handle_event(voltage_frame)
    assert runtime.commands.appliedRowModes == ("VOLT",) * 8
    assert runtime.matrixStore.snapshot().seq == voltage_frame.seq


def test_failed_row_profile_cannot_be_completed_by_later_mixed_data():
    runtime = BackendRuntime(AppConfiguration())
    runtime.commands.request_row_modes(PROFILE, lambda _command: None)
    runtime._handle_event(command_event("accepted", 62, requested=PROFILE))
    runtime._handle_event(command_event("failed", 62, requested=PROFILE, error="route"))

    runtime._handle_event(mixed_frame(201))
    assert runtime.commands.rowModeTransitionState == "error"
    assert runtime.commands.appliedRowModes == ("CAP",) * 8
    assert runtime.commands.rowModeGeneration is None
    assert runtime.matrixStore.snapshot().seq is None
    assert runtime.matrixStore.rejectedWrongMode == 1


def test_failed_global_mode_cannot_be_completed_by_later_measurement_data():
    fixture = Path(__file__).parent / "fixtures" / "current_protocol" / "volt_rows2_mixed.txt"
    envelope = TransportEnvelope(
        source="replay",
        channel="data",
        deviceId="MODE_FAILED",
        sessionGeneration=0,
        receivedMonotonicNs=time.monotonic_ns(),
        receivedWallTime=time.time(),
        rawPayload=fixture.read_bytes(),
    )
    voltage_frame = next(event for event in ProtocolRegistry().feed(envelope) if isinstance(event, MeasurementFrame))
    runtime = BackendRuntime(AppConfiguration())
    runtime.commands.request_mode("VOLT", lambda _command: None)
    runtime._handle_event(
        CommandTransactionEvent(
            commandType="mode",
            phase="accepted",
            requestId=voltage_frame.requestId,
            requestedValue="VOLT",
        )
    )
    runtime._handle_event(
        CommandTransactionEvent(
            commandType="mode",
            phase="failed",
            requestId=voltage_frame.requestId,
            requestedValue="VOLT",
            error="route",
        )
    )

    runtime._handle_event(voltage_frame)
    assert runtime.commands.transitionState == "error"
    assert runtime.commands.appliedMode == "CAP"
    assert runtime.commands.modeGeneration is None
    assert runtime.matrixStore.snapshot().seq is None
    assert runtime.matrixStore.rejectedWrongMode == 1


@pytest.mark.parametrize("rows", range(1, 9))
def test_rows_and_setup_profile_accept_every_integer_one_through_eight(rows: int):
    app = create_app(AppConfiguration())
    with TestClient(app) as client:
        response = client.post("/api/rows", json={"rows": rows})
        profile = client.get("/api/setup/profile").json()
        profile["acquisition"]["rows"] = rows
        profile["acquisition"]["rowModes"] = list(PROFILE)
        applied = client.post("/api/setup/profile", json=profile)
    assert response.status_code == 200
    assert response.json()["rows"] == rows
    assert applied.status_code == 200
    assert applied.json()["profile"]["schemaVersion"] == 3
    assert applied.json()["profile"]["acquisition"]["rows"] == rows
    assert applied.json()["profile"]["acquisition"]["rowModes"] == list(PROFILE)


def test_row_modes_api_sends_one_atomic_command_and_get_reports_pending_then_applied():
    class FakeTransport:
        def __init__(self):
            self.commands: list[str] = []

        def send_command(self, command: str) -> None:
            self.commands.append(command)

        def stop(self) -> None:
            return None

    app = create_app(AppConfiguration())
    with TestClient(app) as client:
        runtime = app.state.runtime
        fake = FakeTransport()
        runtime.transport.current = fake
        runtime.transport.status.update({"transport": "ble", "state": "STREAMING", "sessionGeneration": 0})
        response = client.post("/api/measurement/row-modes", json={"modes": list(PROFILE)})
        pending = client.get("/api/measurement/row-modes").json()
        runtime._handle_event(command_event("accepted", 62, requested=PROFILE))
        runtime._handle_event(command_event("applied", 62, applied=PROFILE, generation=11, seq=201))
        applied = client.get("/api/measurement/row-modes").json()
    assert response.status_code == 200
    assert fake.commands == ["ROWMODES=RVVCCVVR"]
    assert pending["modes"] == ["CAP"] * 8
    assert pending["rowProfile"]["pendingModes"] == list(PROFILE)
    assert applied["modes"] == list(PROFILE)
    assert applied["rowProfile"]["pendingModes"] is None
    assert applied["rowProfile"]["generation"] == 11


def test_homogeneous_rmapp_snapshot_updates_effective_global_mode_without_forging_generation():
    fixture = Path(__file__).parent / "fixtures" / "current_protocol" / "volt_rows2_mixed.txt"
    envelope = TransportEnvelope(
        source="replay",
        channel="data",
        deviceId="ROW_PROFILE_SNAPSHOT",
        sessionGeneration=0,
        receivedMonotonicNs=time.monotonic_ns(),
        receivedWallTime=time.time(),
        rawPayload=fixture.read_bytes(),
    )
    voltage_frame = next(event for event in ProtocolRegistry().feed(envelope) if isinstance(event, MeasurementFrame))
    runtime = BackendRuntime(AppConfiguration())
    runtime.commands.request_row_modes(("VOLT",) * 8, lambda _command: None)
    runtime._handle_event(command_event("accepted", voltage_frame.requestId, requested=("VOLT",) * 8))
    runtime._handle_event(
        command_event(
            "applied",
            voltage_frame.requestId,
            applied=("VOLT",) * 8,
            generation=voltage_frame.generation,
            seq=voltage_frame.seq,
        )
    )
    runtime._handle_event(voltage_frame)
    payload = snapshot_payload(runtime)

    assert payload["measurement"]["appliedMode"] == "VOLT"
    assert payload["measurement"]["transitionState"] == "applied_by_row_profile"
    assert payload["measurement"]["generation"] is None
    assert payload["measurement"]["rowProfile"]["appliedModes"] == ["VOLT"] * 8
    assert payload["measurement"]["rowProfile"]["generation"] == voltage_frame.generation
    assert payload["measurement"]["rowProfile"]["requestId"] == voltage_frame.requestId
    assert payload["matrix"]["mode"] == "VOLT"
    assert payload["matrix"]["quantity"] == "voltage"
    assert payload["matrix"]["modeByRow"] == ["VOLT"] * 8
    assert payload["frame"]["profileGeneration"] == voltage_frame.generation
    assert payload["frame"]["profileRequestId"] == voltage_frame.requestId
    # `rail=1` is the firmware-authoritative age-policy result.  Its cached
    # age may be non-zero without making the newly received telemetry stale.
    assert voltage_frame.railAgeFrames == 3
    assert runtime.telemetry.railTelemetry is not None
    assert runtime.telemetry.railTelemetry.valid is True
    assert runtime.telemetry.railTelemetry.fresh is True
    assert runtime.telemetry.railTelemetry.age == 3


def test_mixed_store_keeps_domain_caches_metadata_and_colour_ranges_isolated():
    runtime = BackendRuntime(AppConfiguration())
    runtime.matrixStore.apply_row_modes(PROFILE, 11, 62, 100)
    assert runtime.matrixStore.add_mixed(mixed_frame(101))
    matrix = runtime.matrixStore.snapshot()
    payload = snapshot_payload(runtime)

    assert matrix.layout == "MIXED"
    assert matrix.unit == ""
    assert matrix.quantity == "resistance"
    assert matrix.domain == "row_specific"
    assert matrix.rowModes == PROFILE
    assert matrix.rowUnits == ("ohm", "V", "V", "pF", "pF", "V", "V", "ohm")
    assert np.isnan(matrix.capValues[0]).all()
    assert np.allclose(matrix.capValues[3], 6.3)
    assert np.allclose(matrix.voltValues[1], 0.2)
    assert np.allclose(matrix.resValues[0], 10_001.0)
    assert payload["frame"]["layout"] == "MIXED"
    assert payload["frame"]["profileGeneration"] == 11
    assert payload["matrix"]["modeByRow"] == list(PROFILE)

    ranges = payload["display"]["colourRanges"]
    assert ranges["cap_absolute"]["max"] < 100.0
    assert ranges["resistance"]["max"] > 10_000.0
    assert ranges["voltage"]["min"] < 0 < ranges["voltage"]["max"]


@pytest.mark.parametrize("value", [10_025.0, 670.0])
def test_single_resistance_value_uses_positive_non_neutral_colour_position(value: float):
    runtime = BackendRuntime(AppConfiguration())
    minimum, maximum = runtime._colour_range_for_domain(
        "resistance",
        np.asarray([value]),
        np.asarray([True]),
    )
    normalized = (value - minimum) / (maximum - minimum)
    assert maximum > minimum
    assert normalized > 0.5


def test_signed_voltage_delta_and_active_row_colour_rules():
    runtime = BackendRuntime(AppConfiguration())
    assert runtime._colour_range_for_domain("voltage", np.asarray([0.25]), np.asarray([True])) == (-0.2625, 0.2625)
    assert runtime._colour_range_for_domain("cap_delta", np.asarray([-2.0]), np.asarray([True])) == (-2.1, 2.1)

    matrix = np.zeros((8, 8), dtype=np.float64)
    matrix[0, 0] = 10.0
    matrix[1, 0] = 1_000_000.0
    usable = np.ones((8, 8), dtype=bool)
    metadata = SimpleNamespace(activeRows=1, rowModes=("RES",) * 8)
    ranges = runtime.colour_ranges(metadata, matrix, usable)
    assert ranges["resistance"]["max"] < 100.0


def test_history_inserts_mode_discontinuity_for_same_cell():
    history = MatrixHistoryStore(10)
    for seq, mode, value in ((1, "CAP", 6.0), (2, "RES", 10_000.0), (3, "CAP", 6.3)):
        values = np.full(64, np.nan)
        valid = np.zeros(64, dtype=bool)
        values[0] = value
        valid[0] = True
        history.append(
            seq,
            float(seq),
            values,
            valid,
            1,
            mode="MIXED",
            row_modes=(mode,) * 8,
            row_units=(("pF" if mode == "CAP" else "ohm"),) * 8,
            row_scales=((-6 if mode == "CAP" else -3),) * 8,
        )
    result = history.slice([0], measurement_mode="CAP")
    assert result.seq.tolist() == [1, 2, 3]
    assert result.valid[:, 0].tolist() == [True, False, True]
    assert np.isnan(result.values[1, 0])


def test_mixed_baseline_and_delta_touch_only_cap_rows():
    runtime = BackendRuntime(AppConfiguration())
    # Keep this assertion focused on the CAP baseline transform.  The default
    # voltage view is VSS-relative and correctly becomes unavailable without
    # same-boot rail telemetry.
    runtime.ui.voltageReference = "ground"
    runtime.transport.status.update({"transport": "serial", "device": ""})
    runtime._handle_event(mixed_frame(1, cap_value=6.0))
    runtime.capture_baseline()
    assert runtime._baseline_session is not None
    runtime._baseline_session.minSamples = 1
    runtime._handle_event(mixed_frame(2, cap_value=6.3))
    baseline = runtime._baseline_session.complete()
    runtime.ui.baseline = baseline
    runtime.ui.displayMode = DisplayMode.DELTA_PERCENT
    runtime._baseline_session = None
    runtime._handle_event(mixed_frame(3, cap_value=6.93))
    matrix = runtime.matrixStore.snapshot()
    display = _display_matrix(runtime, matrix)

    assert not baseline.validMask[:24].any()
    assert baseline.validMask[24:40].all()
    assert not baseline.validMask[40:].any()
    assert display[3, 0] == pytest.approx(10.0)
    assert display[0, 0] == pytest.approx(10_001.0)
    assert display[1, 0] == pytest.approx(0.2)


def test_zero_current_offsets_in_mixed_frame_updates_only_active_cap_cells():
    runtime = BackendRuntime(AppConfiguration())
    runtime.matrixStore.apply_row_modes(PROFILE, 11, 62, 100)
    assert runtime.matrixStore.add_mixed(mixed_frame(101, cap_value=6.3))
    result = runtime.zero_current_offsets("all")
    offsets = np.asarray(result["offsetsPf"], dtype=np.float64)

    assert result["changedCells"] == 16
    assert np.allclose(offsets[3:5, :], 6.3)
    assert np.count_nonzero(offsets[:3, :]) == 0
    assert np.count_nonzero(offsets[5:, :]) == 0
    with pytest.raises(ValueError, match="not an active capacitance row"):
        runtime.zero_current_offsets("row", row=1)


def test_battery_latest_last_good_firmware_preference_and_device_isolation():
    store = TelemetryStore()
    store.begin_device("ble:board-a")
    store.update_battery(parse_battery_fields({"bt": "4092", "valid": "1", "fresh": "1", "reason": "ok"}, 100.0))
    store.update_battery(
        parse_battery_fields(
            {
                "bt": "-1",
                "valid": "0",
                "fresh": "0",
                "reason": "adc_timeout",
                "lastGoodMv": "4092",
                "lastGoodValid": "1",
                "lastGoodFresh": "1",
                "lastGoodAgeMs": "1800",
                "lastGoodFrame": "88",
            },
            101.0,
        )
    )
    failed = store.battery_snapshot(102.0)
    assert failed["batteryMv"] == 4092
    assert failed["batteryText"] == "4.092 V"
    assert failed["fresh"] is False
    assert failed["reason"] == "adc_timeout"
    assert failed["latestAttempt"]["valid"] is False
    assert failed["lastGood"]["firmwareAuthoritative"] is True
    assert failed["lastGood"]["fresh"] is True
    assert failed["lastGood"]["frame"] == 88

    store.begin_device("ble:board-a")
    assert store.battery_snapshot(103.0)["batteryMv"] == 4092
    store.begin_device("ble:board-b")
    assert store.battery_snapshot(104.0)["batteryMv"] is None
    assert store.battery_snapshot(104.0)["available"] is False


def test_battery_snapshot_preserves_legacy_flat_attempt_diagnostics():
    store = TelemetryStore()
    store.begin_device("ble:board-a")
    store.update_battery(
        parse_battery_fields(
            {
                "bt": "4012",
                "valid": "1",
                "fresh": "1",
                "retry": "0/1",
                "unstable": "1",
                "spreadRaw": "5",
                "spreadMaxRaw": "9",
            },
            100.0,
        )
    )

    payload = store.battery_snapshot(101.0)
    assert payload["retryCount"] == 0
    assert payload["retryLimit"] == 1
    assert payload["unstableCount"] == 1
    assert payload["spreadRaw"] == 5
    assert payload["spreadMaximumRaw"] == 9
    assert payload["latestAttempt"]["spreadMaximumRaw"] == 9


def test_rail_span_is_retained_but_marked_stale_across_same_device_gap():
    store = TelemetryStore()
    store.begin_device("ble:board-a")
    store.update_rail(
        RailTelemetry(
            railSpanUv=5_126_000,
            valid=True,
            fresh=True,
            age=0,
            ageMs=100,
            source="internal_monitor",
            reason="ok",
            timestamp=100.0,
        )
    )
    assert store.rail_snapshot(101.0)["fresh"] is True
    store.mark_connection_stale()
    stale = store.rail_snapshot(102.0)
    assert stale["railSpanUv"] == 5_126_000
    assert stale["valid"] is True
    assert stale["fresh"] is False
    assert stale["reason"] == "connection_stale"


def test_fresh_rail_does_not_relabel_retained_battery_as_fresh_after_gap():
    store = TelemetryStore()
    store.begin_device("ble:board-a")
    store.update_battery(parse_battery_fields({"bt": "4092", "valid": "1", "fresh": "1", "reason": "ok"}, 100.0))
    store.mark_connection_stale()
    store.update_rail(
        RailTelemetry(
            railSpanUv=5_126_000,
            valid=True,
            fresh=True,
            age=0,
            ageMs=0,
            source="internal_monitor",
            reason="ok",
            timestamp=101.0,
        )
    )
    assert store.rail_snapshot(102.0)["fresh"] is True
    assert store.battery_snapshot(102.0)["fresh"] is False


def test_same_device_connection_gap_reports_battery_as_last_known_stale_not_ok():
    store = TelemetryStore()
    store.begin_device("ble:board-a")
    store.update_battery(parse_battery_fields({"bt": "4092", "valid": "1", "fresh": "1", "reason": "ok"}, 100.0))
    store.mark_connection_stale()

    payload = store.battery_snapshot(102.0)
    assert payload["batteryMv"] == 4092
    assert payload["fresh"] is False
    assert payload["lastKnown"] is True
    assert payload["state"] == "stale"
    assert payload["reason"] == "connection_stale"


def test_valid_but_nonfresh_battery_attempt_reports_stale_reason():
    store = TelemetryStore()
    store.begin_device("ble:board-a")
    store.update_battery(parse_battery_fields({"bt": "4092", "valid": "1", "fresh": "1", "reason": "ok"}, 100.0))
    store.update_battery(parse_battery_fields({"bt": "4092", "valid": "1", "fresh": "0", "reason": "ok"}, 101.0))

    payload = store.battery_snapshot(102.0)
    assert payload["batteryMv"] == 4092
    assert payload["state"] == "stale"
    assert payload["reason"] == "stale"


def test_explicit_invalid_firmware_last_good_overrides_host_fallback():
    store = TelemetryStore()
    store.begin_device("ble:board-a")
    store.update_battery(parse_battery_fields({"bt": "4092", "valid": "1", "fresh": "1"}, 100.0))
    assert store.battery_snapshot(100.0)["batteryMv"] == 4092
    store.update_battery(
        parse_battery_fields(
            {"bt": "-1", "valid": "0", "fresh": "0", "bl": "-1", "blValid": "0", "reason": "adc_timeout"},
            101.0,
        )
    )
    snapshot = store.battery_snapshot(101.0)
    assert snapshot["available"] is False
    assert snapshot["batteryMv"] is None
