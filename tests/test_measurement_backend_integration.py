from __future__ import annotations

import time
import queue
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from sensorarray_app.app.configuration import AppConfiguration
from sensorarray_app.domain.models import (
    CapacitanceFrame,
    CommandTransactionEvent,
    MeasurementFrame,
    ParserErrorEvent,
    TransportEnvelope,
    TransportStateEvent,
)
from sensorarray_app.protocol.log_protocol import TextLogProtocol
from sensorarray_app.protocol.registry import ProtocolRegistry
from sensorarray_app.services.command_service import CommandService
from sensorarray_app.store.matrix_store import MatrixStore
from sensorarray_app.transport.manager import TransportManager
from sensorarray_backend.core.runtime import BackendRuntime
from sensorarray_backend.core.session_data import (
    SessionFrame,
    frames_to_measurement_ascii_bytes,
    load_session_frames,
)
from sensorarray_backend.core.snapshot import snapshot_payload


FIXTURES = Path(__file__).parent / "fixtures" / "current_protocol"


def _envelope(payload: bytes, *, session_generation: int = 0) -> TransportEnvelope:
    return TransportEnvelope(
        source="replay",
        channel="data",
        deviceId="INTEGRATION_TEST",
        sessionGeneration=session_generation,
        receivedMonotonicNs=time.monotonic_ns(),
        receivedWallTime=time.time(),
        rawPayload=payload,
    )


def _measurement_fixture(name: str) -> MeasurementFrame:
    events = ProtocolRegistry().feed(_envelope((FIXTURES / name).read_bytes()))
    frames = [event for event in events if isinstance(event, MeasurementFrame)]
    assert len(frames) == 1
    return frames[0]


def _command_event(
    command_type: str,
    phase: str,
    request_id: int,
    *,
    old=None,
    requested=None,
    applied=None,
    generation: int | None = None,
    seq: int | None = None,
    state: str | None = None,
    error: str | None = None,
    raw_fields: dict[str, str] | None = None,
    session_generation: int = 0,
) -> CommandTransactionEvent:
    return CommandTransactionEvent(
        commandType=command_type,
        phase=phase,
        requestId=request_id,
        oldValue=old,
        requestedValue=requested,
        appliedValue=applied,
        generation=generation,
        frameSeq=seq,
        state=state,
        error=error,
        rawFields=raw_fields or {},
        sessionGeneration=session_generation,
        rawText=f"{command_type}:{phase}:{request_id}",
    )


def _cap_frame(seq: int, generation: int, request_id: int, value: float = 12.0) -> CapacitanceFrame:
    cells = 8
    corrected = np.full(cells, value, dtype=np.float64)
    raw_pf = corrected + 33.0
    return CapacitanceFrame(
        seq=seq,
        timestampUs=seq * 1000,
        rows=1,
        cells=cells,
        generation=generation,
        requestId=request_id,
        rowFreshMask=0x01,
        primaryFreshMask=0x01,
        secondaryFreshMask=0x01,
        badStaleCount=0,
        badMixedCount=0,
        badInvalidCount=0,
        rawFixedValues=np.rint(raw_pf * 1_000_000).astype(np.int64),
        rawPfValues=raw_pf,
        correctedPfValues=corrected,
        validMask=np.ones(cells, dtype=bool),
        sourceTransport="replay",
        sessionGeneration=0,
        receivedTime=time.time(),
        receivedMonotonicNs=time.monotonic_ns(),
    )


class _CommandTransport:
    source = "serial"

    def __init__(self) -> None:
        self.commands: list[str] = []

    def send_command(self, command: str) -> None:
        self.commands.append(command)

    def stop(self) -> None:
        return None


def _dispatch_log(runtime: BackendRuntime, line: str, *, session_generation: int = 0) -> None:
    envelope = replace(_envelope(b"", session_generation=session_generation), channel="log")
    for event in TextLogProtocol().feed_line(line, envelope):
        runtime._handle_event(event)


def test_mode_transaction_never_commits_on_mack_and_requires_matching_mapp() -> None:
    sent: list[str] = []
    service = CommandService()
    service.request_mode("VOLT", sent.append)
    assert sent == ["MODE=VOLT"]
    assert service.appliedMode == "CAP"
    assert service.pendingMode == "VOLT"

    service.handle(_command_event("mode", "accepted", 42, requested="VOLT"))
    assert service.appliedMode == "CAP"
    assert service.pendingMode == "VOLT"
    assert service.transitionState == "accepted"

    service.handle(_command_event("mode", "accepted", 41, requested="RES"))
    assert service.pendingMode == "VOLT"
    assert service.modeRequestId == 42
    service.handle(_command_event("mode", "accepted", 43, requested="VOLT"))
    assert service.modeRequestId == 42

    service.handle(_command_event("mode", "applied", 99, requested="VOLT", applied="VOLT", generation=7, seq=8))
    assert service.appliedMode == "CAP"
    assert service.pendingMode == "VOLT"

    applied = service.handle(
        _command_event("mode", "applied", 42, requested="VOLT", applied="VOLT", generation=7, seq=8)
    )
    assert applied["modeApplied"] is True
    assert service.appliedMode == "VOLT"
    assert service.pendingMode is None
    assert (service.modeRequestId, service.modeGeneration, service.modeFrameSeq) == (42, 7, 8)


def test_mode_error_keeps_last_applied_value_but_releases_pending_gate() -> None:
    service = CommandService()
    service.request_mode("VOLT", lambda _command: None)
    service.handle(_command_event("mode", "accepted", 42, requested="VOLT"))
    service.handle(
        _command_event("mode", "failed", 42, requested="VOLT", state="SAFE", error="0x103")
    )
    assert service.appliedMode == "CAP"
    assert service.pendingMode is None
    assert service.transitionState == "error"
    assert service.deviceState == "SAFE"
    assert "SAFE" in service.modeError
    assert "0x103" in service.modeError


def test_new_transport_generation_resets_mode_and_ignores_old_session_apply() -> None:
    service = CommandService()
    service.observe_mode_frame("VOLT", 7, 42, 8)
    assert service.appliedMode == "VOLT"
    service.reset_session(5)
    assert service.appliedMode == "CAP"
    ignored = service.handle(
        _command_event(
            "mode", "applied", 42, requested="VOLT", applied="VOLT", generation=7, seq=8,
            session_generation=4,
        )
    )
    assert ignored["ignoredOldSession"] is True
    assert service.appliedMode == "CAP"


def test_modern_store_rejects_preboundary_old_generation_and_wrong_request_id() -> None:
    frame = _measurement_fixture("volt_rows2_mixed.txt")
    store = MatrixStore()
    store.apply_measurement_mode("VOLT", frame.generation, frame.requestId, frame.seq)
    assert store.add_measurement(frame)
    initial_revision = store.snapshot().revision

    assert not store.add_measurement(replace(frame, seq=9, generation=6))
    assert not store.add_measurement(replace(frame, seq=9, requestId=99))
    assert not store.add_measurement(replace(frame, seq=7))
    assert store.snapshot().revision == initial_revision
    assert store.rejectedStaleGeneration == 2
    assert store.rejectedBeforeBoundary == 1

    changed_values = frame.physicalValues.copy()
    changed_values[0] = 0.5
    assert store.add_measurement(replace(frame, seq=10, physicalValues=changed_values))
    assert store.snapshot().matrix[0, 0] == pytest.approx(0.5)


def test_late_old_mode_frame_cannot_reverse_a_mapp_applied_mode() -> None:
    runtime = BackendRuntime(AppConfiguration())
    runtime.commands.request_mode("VOLT", lambda _command: None)
    runtime._handle_event(_command_event("mode", "accepted", 42, requested="VOLT"))
    runtime._handle_event(
        _command_event(
            "mode", "applied", 42, requested="VOLT", applied="VOLT", generation=7, seq=8
        )
    )
    assert runtime.commands.appliedMode == "VOLT"

    runtime._handle_event(_measurement_fixture("res_rows1_mixed.txt"))
    assert runtime.commands.appliedMode == "VOLT"
    assert runtime.matrixStore.appliedMode == "VOLT"
    assert runtime.matrixStore.snapshot().seq is None
    assert runtime.matrixStore.rejectedWrongMode == 1

    runtime._handle_event(_measurement_fixture("volt_rows2_mixed.txt"))
    assert runtime.matrixStore.snapshot().seq == 8


def test_replay_can_observe_multiple_distinct_mode_transactions_in_one_session() -> None:
    runtime = BackendRuntime(AppConfiguration())
    runtime.commands.reset_session(0)
    for event in (
        _command_event("mode", "accepted", 42, old="CAP", requested="VOLT"),
        _command_event("mode", "applied", 42, old="CAP", requested="VOLT", applied="VOLT", generation=7, seq=8),
        _command_event("mode", "accepted", 43, old="VOLT", requested="RES"),
        _command_event("mode", "applied", 43, old="VOLT", requested="RES", applied="RES", generation=8, seq=9),
        _command_event("mode", "accepted", 44, old="RES", requested="CAP"),
        _command_event("mode", "applied", 44, old="RES", requested="CAP", applied="CAP", generation=9, seq=10),
    ):
        runtime._handle_event(event)
    assert runtime.commands.appliedMode == "CAP"
    assert runtime.commands.modeRequestId == 44
    assert runtime.commands.modeGeneration == 9


def test_cap_mode_boundary_uses_sequence_not_cap_rows_generation_or_request_id() -> None:
    store = MatrixStore()
    store.apply_measurement_mode("CAP", generation=55, request_id=99, frame_seq=20)
    assert not store.add_capacitance(_cap_frame(19, generation=55, request_id=99))
    assert store.add_capacitance(_cap_frame(20, generation=999, request_id=888))
    snapshot = store.snapshot()
    assert snapshot.mode == "CAP"
    assert snapshot.seq == 20
    assert snapshot.firmwareGeneration == 999
    assert snapshot.requestId == 888


@pytest.mark.parametrize(
    ("avdd_uv", "avss_uv"),
    [(0, -2_500_000), (3_300_000, 0), (3_300_000, -100_000), (4_000_000, -2_500_001)],
)
def test_rail_configuration_rejects_bad_sign_or_span(avdd_uv: int, avss_uv: int) -> None:
    with pytest.raises(ValueError):
        CommandService().request_rail(avdd_uv, avss_uv, lambda _command: None)


def test_voltage_request_sends_only_mode_and_mapp_remains_the_commit_boundary() -> None:
    runtime = BackendRuntime(AppConfiguration())
    transport = _CommandTransport()
    runtime.transport.current = transport
    runtime.transport.status.update({"transport": "serial", "state": "STREAMING", "sessionGeneration": 0})

    runtime.request_measurement_mode_api("VOLT")
    assert transport.commands == ["MODE=VOLT"]
    assert runtime.commands.appliedMode == "CAP"
    assert runtime.commands.transitionState == "requested"

    _dispatch_log(runtime, "MACK,id=42,old=CAP,new=VOLT,state=accepted")
    assert runtime.commands.appliedMode == "CAP"
    assert runtime.commands.pendingMode == "VOLT"

    _dispatch_log(runtime, "MAPP,id=99,gen=7,old=CAP,new=VOLT,seq=8,state=applied,transitionUs=500")
    assert runtime.commands.appliedMode == "CAP"
    _dispatch_log(runtime, "MAPP,id=42,gen=7,old=CAP,new=VOLT,seq=8,state=applied,transitionUs=500")
    assert runtime.commands.appliedMode == "VOLT"
    assert runtime.matrixStore.appliedMode == "VOLT"
    assert runtime.matrixStore.snapshot().seq is None


def test_legacy_rail_fields_on_mode_request_are_ignored_by_production_flow() -> None:
    runtime = BackendRuntime(AppConfiguration())
    transport = _CommandTransport()
    runtime.transport.current = transport
    runtime.transport.status.update({"transport": "serial", "state": "STREAMING", "sessionGeneration": 0})
    runtime.commands.railConfigured = True
    runtime.commands.railState = "applied"
    runtime.commands.measuredAvddV = 3.391
    runtime.commands.measuredAvssV = -2.5

    runtime.request_measurement_mode_api("VOLT", measured_avdd_v=3.45, measured_avss_v=-2.45)
    assert transport.commands == ["MODE=VOLT"]
    assert runtime.commands.transitionState == "requested"
    assert runtime.commands.measuredAvddV == pytest.approx(3.391)
    assert runtime.commands.measuredAvssV == pytest.approx(-2.5)


def test_explicit_debug_rail_configuration_remains_available() -> None:
    runtime = BackendRuntime(AppConfiguration())
    transport = _CommandTransport()
    runtime.transport.current = transport
    runtime.transport.status.update({"transport": "serial", "state": "STREAMING", "sessionGeneration": 0})
    runtime.commands.railConfigured = True
    runtime.commands.railState = "applied"
    runtime.commands.measuredAvddV = 3.391
    runtime.commands.measuredAvssV = -2.5

    runtime.configure_voltage_rail(3.45, -2.45)
    assert transport.commands == ["RAILCFG=3450000,-2450000"]
    assert runtime.commands.railState == "requested"


def test_rail_apply_timeout_releases_voltage_transition_for_retry() -> None:
    service = CommandService()
    service.request_rail(3_391_000, -2_500_000, lambda _command: None, desired_mode="VOLT")
    service.handle(
        _command_event(
            "rail",
            "accepted",
            51,
            requested={"avddUv": 3_391_000, "avssUv": -2_500_000, "source": "external"},
        )
    )
    service.timeout_old(seconds=-1.0)
    assert service.railState == "timeout"
    assert service.transitionState == "timeout"
    assert service.pendingMode == "VOLT"
    assert service.desiredModeAfterRail is None
    assert "RAPP" in service.modeError


def test_second_accepted_id_cannot_hijack_active_rows_rail_or_ads_transaction() -> None:
    service = CommandService()
    service.request_rows(2, lambda _command: None)
    service.handle(_command_event("rows", "accepted", 4, requested=2))
    service.handle(_command_event("rows", "accepted", 5, requested=2))
    assert service.rowsRequestId == 4

    service.request_rail(3_391_000, -2_500_000, lambda _command: None)
    rail_value = {"avddUv": 3_391_000, "avssUv": -2_500_000, "source": "external"}
    service.handle(_command_event("rail", "accepted", 51, requested=rail_value))
    service.handle(_command_event("rail", "accepted", 52, requested=rail_value))
    assert service.railRequestId == 51

    service.handle(_command_event("ads_check", "accepted", 7, requested=100))
    service.handle(_command_event("ads_check", "accepted", 8, requested=100))
    assert service.adsDiagnostics["requestId"] == 7


def test_rows_and_ads_check_ignore_nonmatching_request_ids() -> None:
    service = CommandService()
    service.request_rows(2, lambda _command: None)
    service.handle(_command_event("rows", "accepted", 4, requested=2))
    service.handle(_command_event("rows", "applied", 5, applied=2, seq=9))
    assert service.activeRows == 8
    assert service.pendingRows == 2
    service.handle(_command_event("rows", "applied", 4, applied=2, seq=10))
    assert service.activeRows == 2
    assert service.pendingRows is None

    service.record_action("ads_check", "ADSCHK=100", 100, lambda _command: None)
    service.handle(_command_event("ads_check", "accepted", 7, requested=100))
    service.handle(
        _command_event(
            "ads_check", "complete", 8, raw_fields={"fresh": "100", "restore": "ok"}
        )
    )
    assert service.adsDiagnostics["state"] == "checking"
    assert service.adsDiagnostics["statistics"] == {}
    service.handle(
        _command_event(
            "ads_check", "complete", 7, raw_fields={"fresh": "100", "restore": "ok"}
        )
    )
    assert service.adsDiagnostics["state"] == "completed"
    assert service.adsDiagnostics["statistics"]["fresh"] == "100"


def test_snapshot_is_quantity_typed_and_excludes_invalid_stale_cells_from_display_range() -> None:
    runtime = BackendRuntime(AppConfiguration())
    runtime.ui.userOffsetsPf[0][0] = 999.0
    frame = _measurement_fixture("volt_rows2_mixed.txt")
    runtime._handle_event(frame)
    payload = snapshot_payload(runtime)

    assert payload["connection"]["transportMode"] == "serial"
    assert payload["measurement"]["appliedMode"] == "VOLT"
    assert payload["matrix"]["quantity"] == "voltage"
    assert payload["matrix"]["wireUnit"] == "V"
    assert payload["matrix"]["scale"] == -6
    assert payload["matrix"]["rawFixed"][0][0] == -1250.0
    assert payload["matrix"]["values"][0][0] == pytest.approx(-0.00125)
    assert payload["matrix"]["displayValues"][0][0] == pytest.approx(-0.00125)
    assert payload["matrix"]["values"][0][4] is None
    assert payload["matrix"]["errorCodes"][0][4] == 0x03
    assert payload["matrix"]["errorReasons"][0][4] == "ADS DRDY timeout"
    assert payload["matrix"]["pga"][0][:7] == [1, 2, 4, 8, 16, 32, 0]
    assert payload["matrix"]["pgaBypass"][0][6] is True
    assert payload["matrix"]["fresh"][1][1] is False
    assert payload["capacitance"]["available"] is False
    assert payload["display"]["colorRange"]["min"] != 999.0

    runtime.capture_baseline()
    assert runtime.ui.baseline is None
    assert runtime.ui.baselineInvalidReason == "Available when an active row uses CAP"
    with pytest.raises(ValueError, match="active row uses CAP"):
        runtime.set_display_mode("delta_percent")


@pytest.mark.parametrize("fixture_name", ["volt_rows2_mixed.txt", "res_rows1_mixed.txt"])
@pytest.mark.parametrize("fmt", ["csv", "xlsx", "mat", "h5"])
def test_voltage_and_resistance_export_import_round_trip(
    tmp_path: Path,
    fixture_name: str,
    fmt: str,
) -> None:
    source_runtime = BackendRuntime(AppConfiguration())
    frame = _measurement_fixture(fixture_name)
    source_runtime._handle_event(frame)
    data, _content_type, extension = source_runtime.export_session_file(fmt)
    path = tmp_path / f"{frame.mode.lower()}.{extension}"
    path.write_bytes(data)

    loaded = load_session_frames(path)
    restored = loaded[-1]
    assert restored.mode == frame.mode
    assert restored.unit == frame.unit
    assert restored.scale == frame.scale
    assert restored.source == frame.sourceTransport
    assert restored.generation == frame.generation
    assert restored.requestId == frame.requestId
    np.testing.assert_allclose(
        restored.physical_values()[: frame.cells], frame.physicalValues, equal_nan=True
    )
    np.testing.assert_allclose(
        restored.raw_fixed_values()[: frame.cells], frame.rawFixedValues, equal_nan=True
    )
    np.testing.assert_array_equal(restored.valid[: frame.cells], frame.validMask)
    np.testing.assert_array_equal(restored.fresh_values()[: frame.cells], frame.freshMask)
    expected_error_codes = np.where(
        frame.errorMask, frame.errorCodes.astype(np.int16), -1
    )
    np.testing.assert_array_equal(
        restored.error_code_values()[: frame.cells], expected_error_codes
    )
    np.testing.assert_array_equal(restored.pga_values()[: frame.cells], frame.pgaValues)

    imported_runtime = BackendRuntime(AppConfiguration())
    imported_runtime.commands.request_mode("RES", lambda _command: None)
    assert imported_runtime.commands.pendingMode == "RES"
    result = imported_runtime.import_session_file(str(path))
    snapshot = snapshot_payload(imported_runtime)
    assert result["measurementMode"] == frame.mode
    assert snapshot["measurement"]["appliedMode"] == frame.mode
    assert snapshot["matrix"]["mode"] == frame.mode
    assert snapshot["matrix"]["values"][0][0] == pytest.approx(float(frame.physicalValues[0]))
    assert snapshot["matrix"]["pga"][0][0] == int(frame.pgaValues[0])
    assert snapshot["measurement"]["pendingMode"] is None
    assert snapshot["measurement"]["transitionState"] in {"applied", "synced"}


def test_replay_transport_registry_store_chain_uses_current_voltage_fixture() -> None:
    runtime = BackendRuntime(AppConfiguration())
    runtime.start()
    try:
        runtime.open_replay(str(FIXTURES / "volt_rows2_mixed.txt"), speed=100.0)
        runtime.start_replay()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and runtime.matrixStore.snapshot().seq != 8:
            time.sleep(0.01)
        snapshot = snapshot_payload(runtime)
        assert snapshot["matrix"]["mode"] == "VOLT"
        assert snapshot["matrix"]["values"][0][0] == pytest.approx(-0.00125)
        assert snapshot["matrix"]["errorCodes"][0][4] == 0x03
        assert snapshot["matrix"]["pgaBypass"][0][6] is True
    finally:
        runtime.stop()


def test_replay_exported_mode_change_uses_synthetic_mack_mapp_boundary(tmp_path: Path) -> None:
    cap_values = np.full(64, np.nan)
    cap_values[:8] = 10.0
    cap_valid = np.zeros(64, dtype=bool)
    cap_valid[:8] = True
    cap_frame = SessionFrame(
        seq=7,
        timeSeconds=0.007,
        rows=1,
        valuesPf=cap_values,
        valid=cap_valid,
        measurementMode="CAP",
        physicalValues=cap_values,
        fresh=cap_valid,
        generation=3,
        requestId=4,
        source="replay",
    )
    voltage = _measurement_fixture("volt_rows2_mixed.txt")
    volt_values = np.full(64, np.nan)
    volt_values[: voltage.cells] = voltage.physicalValues
    volt_valid = np.zeros(64, dtype=bool)
    volt_valid[: voltage.cells] = voltage.validMask
    volt_fresh = np.zeros(64, dtype=bool)
    volt_fresh[: voltage.cells] = voltage.freshMask
    volt_raw = np.full(64, np.nan)
    volt_raw[: voltage.cells] = voltage.rawFixedValues
    volt_errors = np.full(64, -1, dtype=np.int16)
    volt_errors[: voltage.cells] = np.where(
        voltage.errorMask, voltage.errorCodes.astype(np.int16), -1
    )
    volt_pga = np.full(64, -1, dtype=np.int16)
    volt_pga[: voltage.cells] = voltage.pgaValues
    volt_frame = SessionFrame(
        seq=voltage.seq,
        timeSeconds=voltage.timestampUs / 1_000_000.0,
        rows=voltage.rows,
        valuesPf=np.full(64, np.nan),
        valid=volt_valid,
        measurementMode="VOLT",
        unit="V",
        scale=-6,
        physicalValues=volt_values,
        rawFixed=volt_raw,
        fresh=volt_fresh,
        errorCodes=volt_errors,
        pga=volt_pga,
        generation=voltage.generation,
        requestId=voltage.requestId,
        source="replay",
    )
    replay_path = tmp_path / "cap_to_volt.txt"
    replay_bytes = frames_to_measurement_ascii_bytes([cap_frame, volt_frame])
    assert b"MACK,id=42,old=CAP,new=VOLT,state=accepted\n" in replay_bytes
    assert b"MAPP,id=42,gen=7,old=CAP,new=VOLT,seq=8,state=applied" in replay_bytes
    replay_path.write_bytes(replay_bytes)

    runtime = BackendRuntime(AppConfiguration())
    runtime.start()
    try:
        runtime.open_replay(str(replay_path), speed=100.0)
        runtime.start_replay()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and runtime.matrixStore.snapshot().mode != "VOLT":
            time.sleep(0.01)
        snapshot = snapshot_payload(runtime)
        assert snapshot["measurement"]["appliedMode"] == "VOLT"
        assert snapshot["measurement"]["requestId"] == 42
        assert snapshot["matrix"]["mode"] == "VOLT"
        assert snapshot["matrix"]["values"][0][0] == pytest.approx(-0.00125)
        history_modes = runtime.matrixStore.history.modes[
            runtime.matrixStore.history.ordered_indices()
        ].tolist()
        assert history_modes == ["CAP", "VOLT"]
    finally:
        runtime.stop()


def test_crc_bad_frame_never_updates_store_and_next_frame_recovers() -> None:
    runtime = BackendRuntime(AppConfiguration())
    good = (FIXTURES / "volt_rows2_mixed.txt").read_bytes()
    bad = good.replace(b"D0,-1250", b"D0,-1251", 1)

    bad_events = runtime.registry.feed(_envelope(bad))
    assert any(isinstance(event, ParserErrorEvent) and event.reason == "crc" for event in bad_events)
    for event in bad_events:
        runtime._handle_event(event)
    assert runtime.matrixStore.snapshot().seq is None
    assert runtime.stats.crcFailures == 1

    for event in runtime.registry.feed(_envelope(good)):
        runtime._handle_event(event)
    snapshot = runtime.matrixStore.snapshot()
    assert snapshot.mode == "VOLT"
    assert snapshot.seq == 8
    assert snapshot.matrix[0, 0] == pytest.approx(-0.00125)


def test_runtime_drops_envelope_from_previous_transport_generation() -> None:
    runtime = BackendRuntime(AppConfiguration())
    runtime.commands.reset_session(2)
    runtime.transport.status.update({"sessionGeneration": 2, "transport": "replay"})
    runtime.start()
    try:
        packet = (FIXTURES / "volt_rows2_mixed.txt").read_bytes()
        runtime.inputQueue.put(_envelope(packet, session_generation=1))
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if runtime.stats.snapshot(0.0)["parserRejects"]:
                break
            time.sleep(0.01)
        assert runtime.matrixStore.snapshot().seq is None

        runtime.inputQueue.put(_envelope(packet, session_generation=2))
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and runtime.matrixStore.snapshot().seq != 8:
            time.sleep(0.01)
        assert runtime.matrixStore.snapshot().seq == 8
    finally:
        runtime.stop()


def test_new_transport_session_clears_battery_ads_and_rate_fallbacks() -> None:
    runtime = BackendRuntime(AppConfiguration())
    _dispatch_log(runtime, "ABAT,bt=4012,valid=1,fresh=1,ageMs=1,rail=5200000,railState=ok,reason=ok")
    _dispatch_log(runtime, "ADS,chip=1262,valid=1")
    _dispatch_log(runtime, "SF50,cfps=200.0,efps=50.0,ofps=5.0/50.0/50.0")
    assert snapshot_payload(runtime)["battery"]["batteryMv"] == 4012
    assert snapshot_payload(runtime)["ads"]["identityConfirmed"] is True
    assert snapshot_payload(runtime)["ads"]["chip"] == "1262"

    runtime.transport.status.update({"sessionGeneration": 1, "transport": "serial"})
    runtime.start()
    try:
        runtime.inputQueue.put(TransportStateEvent("serial", "STREAMING", 1, "new device"))
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and runtime.commands.sessionGeneration != 1:
            time.sleep(0.01)
        snapshot = snapshot_payload(runtime)
        assert snapshot["battery"]["batteryText"] == "N/A"
        assert snapshot["battery"]["available"] is False
        assert snapshot["ads"]["identityAvailable"] is False
        assert snapshot["ads"]["identityConfirmed"] is None
        assert snapshot["ads"]["chip"] == "unknown"
        assert snapshot["rates"]["captureFps"] is None
        assert snapshot["rates"]["emittedFps"] is None
    finally:
        runtime.stop()


def test_transport_state_information_is_not_reported_as_an_error() -> None:
    manager = TransportManager(queue.Queue())
    manager.status["sessionGeneration"] = 3

    manager.apply_state_event(TransportStateEvent("replay", "STREAMING", 3, r"C:\fixtures\cap.replay"))
    assert manager.status["message"] == r"C:\fixtures\cap.replay"
    assert manager.status["error"] == ""

    manager.apply_state_event(TransportStateEvent("replay", "ERROR", 3, "fixture CRC failure"))
    assert manager.status["error"] == "fixture CRC failure"
    manager.disconnect()
    assert manager.status["state"] == "DISCONNECTED"
    assert manager.status["device"] == ""
    assert manager.status["message"] == ""
    assert manager.status["error"] == ""


def test_cap_session_rebuild_keeps_freshness_independent_from_invalid_cell() -> None:
    values = np.full(64, np.nan)
    values[:8] = np.arange(8, dtype=np.float64)
    valid = np.zeros(64, dtype=bool)
    valid[:8] = True
    valid[2] = False
    fresh = np.zeros(64, dtype=bool)
    fresh[:8] = True
    frame = SessionFrame(
        seq=5,
        timeSeconds=0.005,
        rows=1,
        valuesPf=values,
        valid=valid,
        measurementMode="CAP",
        physicalValues=values,
        fresh=fresh,
        generation=99,
        requestId=88,
    )
    events = ProtocolRegistry().feed(_envelope(frames_to_measurement_ascii_bytes([frame])))
    parsed = [event for event in events if isinstance(event, CapacitanceFrame)]
    assert len(parsed) == 1
    assert parsed[0].rowFreshMask == 0x01
    assert parsed[0].primaryFreshMask == 0x01
    assert parsed[0].secondaryFreshMask == 0x01
    assert not parsed[0].validMask[2]


def test_cap_session_rebuild_preserves_primary_stale_secondary_fresh() -> None:
    values = np.full(64, np.nan)
    values[:8] = np.arange(8, dtype=np.float64)
    valid = np.zeros(64, dtype=bool)
    valid[:8] = True
    fresh = np.zeros(64, dtype=bool)
    fresh[4:8] = True
    frame = SessionFrame(
        seq=6,
        timeSeconds=0.006,
        rows=1,
        valuesPf=values,
        valid=valid,
        measurementMode="CAP",
        physicalValues=values,
        fresh=fresh,
        generation=5,
        requestId=6,
    )
    payload = frames_to_measurement_ascii_bytes([frame])
    assert b"rf=01,pf=00,sf=01" in payload
    events = ProtocolRegistry().feed(_envelope(payload))
    parsed = [event for event in events if isinstance(event, CapacitanceFrame)]
    assert len(parsed) == 1
    assert parsed[0].rowFreshMask == 0x01
    assert parsed[0].primaryFreshMask == 0x00
    assert parsed[0].secondaryFreshMask == 0x01

    store = MatrixStore()
    assert store.add_capacitance(parsed[0])
    snapshot = store.snapshot()
    assert snapshot.fresh[0, :4].tolist() == [False] * 4
    assert snapshot.fresh[0, 4:].tolist() == [True] * 4


def test_setup_profiles_keep_v1_compatibility_and_v2_measurement_fields() -> None:
    runtime = BackendRuntime(AppConfiguration())
    result_v1 = runtime.apply_setup_profile({"schemaVersion": 1, "transport": {}, "display": {}})
    assert result_v1["ok"] is True
    assert runtime.preferredMeasurementMode == "CAP"
    assert runtime.measuredAvddV is None
    assert runtime.measuredAvssV is None

    result_v2 = runtime.apply_setup_profile(
        {
            "schemaVersion": 2,
            "transport": {},
            "display": {},
            "acquisition": {"measurementMode": "RES"},
            "voltageRail": {"measuredAvddV": 3.391, "measuredAvssV": -2.5},
        }
    )
    profile = result_v2["profile"]
    assert profile["acquisition"]["measurementMode"] == "RES"
    assert profile["voltageRail"] == {"measuredAvddV": 3.391, "measuredAvssV": -2.5}

    with pytest.raises(ValueError, match="CAP, VOLT, or RES"):
        runtime.apply_setup_profile(
            {"schemaVersion": 2, "acquisition": {"measurementMode": "TEMPERATURE"}}
        )
