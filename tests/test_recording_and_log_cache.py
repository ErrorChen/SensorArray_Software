from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import threading
import time

from sensorarray_app.domain.models import LogRecord, MeasurementFrame, TransportEnvelope
from sensorarray_app.protocol.measurement_ascii import MeasurementAsciiParser
from sensorarray_app.services.recording_service import ScientificRecorder, frame_to_v3_record
from sensorarray_app.store.raw_log_store import RawLogStore


FIXTURES = Path(__file__).parent / "fixtures" / "current_protocol"


def _record(tag: str, category: str, text: str | None = None) -> LogRecord:
    return LogRecord(
        timestamp=time.time(),
        monotonicTime=time.monotonic_ns(),
        source="serial",
        channel="log",
        tag=tag,
        severity="info",
        rawText=text or f"{tag},value=1",
        parsedFields={"value": "1"},
        recognised=True,
        sessionGeneration=1,
        connectionGeneration=2,
        bootId=9,
        category=category,
    )


def _voltage_frame() -> MeasurementFrame:
    envelope = TransportEnvelope(
        source="serial",
        channel="data",
        deviceId="RECORDER_TEST",
        sessionGeneration=4,
        connectionGeneration=6,
        receivedMonotonicNs=time.monotonic_ns(),
        receivedWallTime=time.time(),
        rawPayload=(FIXTURES / "volt_rows2_mixed.txt").read_bytes(),
        bootId=12,
    )
    events = MeasurementAsciiParser().feed(envelope)
    frame = next(event for event in events if isinstance(event, MeasurementFrame))
    return replace(frame, bootId=12, connectionGeneration=6)


def test_raw_log_clean_exit_flushes_marker_and_next_startup_collects_it(tmp_path: Path):
    first = RawLogStore(cache_root=tmp_path)
    first.add(_record("READY", "LIFECYCLE"))
    session_path = Path(first.snapshot()["diskCache"]["path"])
    first.close(clean=True)

    state = json.loads((session_path / "crash_state.json").read_text(encoding="utf-8"))
    assert state["clean"] is True
    assert state["written"] == 1

    second = RawLogStore(cache_root=tmp_path)
    assert not session_path.exists()
    assert second.snapshot()["diskCache"]["recoverableSessions"] == []
    second.close(clean=True)


def test_raw_log_unclean_session_is_retained_and_exportable(tmp_path: Path):
    first = RawLogStore(cache_root=tmp_path)
    first.add(_record("BOOT", "LIFECYCLE", "BOOT,bootId=9"))
    first.add(_record("MFAULT", "FAULT", "MFAULT,reason=spi"))
    session_id = first.snapshot()["diskCache"]["sessionId"]
    session_path = Path(first.snapshot()["diskCache"]["path"])
    first.close(clean=False)

    second = RawLogStore(cache_root=tmp_path)
    assert str(session_path) in second.snapshot()["diskCache"]["recoverableSessions"]
    exported = tmp_path / "recovered.log"
    assert second.export_recoverable(session_id, exported) == 2
    assert exported.read_text(encoding="utf-8").splitlines() == ["BOOT,bootId=9", "MFAULT,reason=spi"]
    second.close(clean=True)


def test_raw_log_gc_enforces_aggregate_quota_oldest_first(tmp_path: Path):
    now = time.time()
    for name, size, age in (("session-old", 1600, 200), ("session-new", 300, 100)):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "records.jsonl").write_text("x" * size, encoding="utf-8")
        (directory / "crash_state.json").write_text('{"clean": false}', encoding="utf-8")
        os.utime(directory, (now - age, now - age))

    store = RawLogStore(cache_root=tmp_path, cache_quota_bytes=1024)
    assert not (tmp_path / "session-old").exists()
    assert (tmp_path / "session-new").exists()
    assert store.snapshot()["diskCache"]["recoverableSessions"] == [str(tmp_path / "session-new")]
    store.close(clean=True)


def test_raw_log_measurement_visibility_uses_category_and_default_disk_cache_omits_wire_frames(tmp_path: Path):
    store = RawLogStore(cache_root=tmp_path, raw_wire_capture=False)
    for tag in ("C", "V", "R", "M", "MR", "P"):
        store.add(_record(tag, "MEASUREMENT"))
    store.add(_record("MACK", "CONTROL"))
    store.add(_record("BOOT", "LIFECYCLE"))

    assert [row["category"] for row in store.snapshot()["rows"]] == ["CONTROL", "LIFECYCLE"]
    assert len(store.snapshot(show_data=True)["rows"]) == 8
    store.close(clean=False)
    assert [row["category"] for row in store._cache.iter_records()] == ["CONTROL", "LIFECYCLE"]


def test_frame_v3_prefers_frame_local_rail_and_boot_over_stale_global_telemetry():
    frame = _voltage_frame()
    record = frame_to_v3_record(
        frame,
        session_id="recording-test",
        rail={"bootId": 11, "avddUv": 1, "avssUv": 0, "railValid": False, "railFresh": False},
    )
    assert record["connectionGeneration"] == 6
    assert record["bootId"] == 12
    assert record["rail"]["bootId"] == 12
    assert record["rail"]["avddUv"] == 3_391_000
    assert record["rail"]["avssUv"] == -2_500_000
    assert record["rail"]["railSource"] == "frame"


def test_scientific_recorder_writes_frames_events_and_clean_metadata(tmp_path: Path):
    recorder = ScientificRecorder()
    status = recorder.start(tmp_path, {"firmwareBaseline": "8045e9e9"})
    directory = Path(status["directory"])
    assert recorder.record_frame(_voltage_frame(), configured_row_profile=("VOLT",) * 8)
    assert recorder.record_event("TRANSPORT_RECONNECT", {"connectionGeneration": 7})

    final = recorder.stop()
    assert final["state"] == "NOT_RECORDING"
    assert final["receivedFrames"] == final["writtenFrames"] == 1
    assert final["writtenEvents"] == 1
    assert final["droppedFrames"] == 0
    metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    frame = json.loads((directory / "frames.jsonl").read_text(encoding="utf-8"))
    event = json.loads((directory / "events.jsonl").read_text(encoding="utf-8"))
    assert metadata["clean"] is True
    assert metadata["schemaVersion"] == 3
    assert frame["schemaVersion"] == 3
    assert frame["rowModes"][:2] == ["VOLT", "VOLT"]
    assert event["kind"] == "TRANSPORT_RECONNECT"


def test_scientific_recorder_queue_drop_is_explicit_and_finalized(tmp_path: Path):
    recorder = ScientificRecorder(queue_size=1)
    original_run = recorder._run
    gate = threading.Event()

    def delayed_writer() -> None:
        gate.wait(timeout=2.0)
        original_run()

    recorder._run = delayed_writer  # type: ignore[method-assign]
    status = recorder.start(tmp_path)
    frame = _voltage_frame()
    accepted = [recorder.record_frame(frame) for _ in range(recorder.queueSize + 1)]
    assert accepted.count(False) == 1
    assert recorder.snapshot()["droppedFrames"] == 1
    gate.set()
    final = recorder.stop()
    assert final["writtenFrames"] == recorder.queueSize
    assert final["receivedFrames"] == recorder.queueSize + 1
    assert final["writtenEvents"] == 1
    assert final["pendingGapFrames"] == 0
    metadata = json.loads((Path(status["directory"]) / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["droppedFrames"] == 1
    gap = json.loads((Path(status["directory"]) / "events.jsonl").read_text(encoding="utf-8"))
    assert gap["kind"] == "RECORDING_GAP"
    assert gap["droppedFrames"] == 1
    assert gap["firstSeq"] == gap["lastSeq"] == frame.seq
    assert gap["bootId"] == 12
    assert gap["connectionGeneration"] == 6
