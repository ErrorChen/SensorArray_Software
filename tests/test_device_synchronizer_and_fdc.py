from __future__ import annotations

import time

import pytest

from sensorarray_app.app.configuration import AppConfiguration
from sensorarray_app.domain.models import BootInfo, FdcIsolationInfo, TransportEnvelope
from sensorarray_app.protocol.log_protocol import TextLogProtocol
from sensorarray_app.services.device_synchronizer import DeviceSynchronizer
from sensorarray_backend.core.runtime import BackendRuntime


def _envelope() -> TransportEnvelope:
    return TransportEnvelope(
        source="serial",
        channel="log",
        deviceId="SERIAL_TEST",
        sessionGeneration=4,
        receivedMonotonicNs=time.monotonic_ns(),
        receivedWallTime=time.time(),
        rawPayload=b"",
        connectionGeneration=2,
    )


def _feed(synchronizer: DeviceSynchronizer, line: str) -> None:
    for event in TextLogProtocol().feed_line(line, _envelope()):
        synchronizer.handle(event)


def test_serial_bootstrap_is_ordered_and_queries_fdc_authority_after_row_profile():
    sent: list[str] = []
    synchronizer = DeviceSynchronizer(sent.append)
    synchronizer.start("serial", 4, 2)
    _feed(synchronizer, "PROTO,version=1,wires=ascii,ctrlMax=512,dataMax=1536,channels=CTRL/DATA/LOG/LIFECYCLE")
    _feed(synchronizer, "BUILD,idf=5.5.5,target=esp32s3,project=SensorArray,proto=1")
    _feed(synchronizer, "BOOT,boot=3,bootId=9,reset=software,stage=ready,err=0x0,seq=100,heap=1,heapMin=1,guard=normal,ready=1")
    _feed(synchronizer, "READY,ready=1,boot=3,bootId=9,stage=ready,err=0x0")
    _feed(synchronizer, "MODE,active=RES,state=RESISTANCE,fdcSd=low,fdcSdVerified=1,fdcRestartRequired=0")
    _feed(synchronizer, "ROWS,active=8")
    _feed(synchronizer, "ROWMODES,active=RRRRRRRR")
    _feed(synchronizer, "FDCISO,sd=low,verified=1,restartRequired=0")
    _feed(synchronizer, "ABAT,mv=3900,valid=1,fresh=1,reason=ok")
    _feed(synchronizer, "RAIL,valid=1,fresh=1,avdd=3391000,avss=-2500000,source=internal")
    _feed(synchronizer, "CAL,source=1,schema=3,valid=1,boardId=test,hardwareRev=1,payloadLength=32")
    _feed(synchronizer, "ACK,cmd=ST,v=auto")
    _feed(synchronizer, "USBSTREAM,v=FULL,dataEvery=1,diagEvery=15,state=snapshot")

    assert sent == [
        "PROTO?", "BUILD?", "BOOT?", "READY?", "STATE?", "ROWS?", "ROWMODES?",
        "FDCISO?", "BAT?", "RAIL?", "CAL?", "ST=AUTO", "USBSTREAM?",
    ]
    assert synchronizer.status.state == "SYNCED"
    assert synchronizer.fdcIsolation is not None
    assert synchronizer.fdcIsolation.sd == "low"
    assert synchronizer.snapshot()["fdcIsolation"]["verified"] is True


def test_ble_bootstrap_enables_auto_sink_before_querying_btx() -> None:
    sent: list[str] = []
    synchronizer = DeviceSynchronizer(sent.append)
    synchronizer.start("ble", 4, 2)
    _feed(synchronizer, "PROTO,version=1,wires=ascii,ctrlMax=512,dataMax=1536,channels=CTRL/DATA/LOG/LIFECYCLE")
    _feed(synchronizer, "BUILD,idf=5.5.5,target=esp32s3,project=SensorArray,proto=1")
    _feed(synchronizer, "BOOT,boot=3,bootId=9,reset=software,stage=ready,err=0x0,ready=1")
    _feed(synchronizer, "READY,ready=1,boot=3,bootId=9,stage=ready,err=0x0")
    _feed(synchronizer, "MODE,active=CAP,state=CAPACITANCE")
    _feed(synchronizer, "ROWS,active=8")
    _feed(synchronizer, "ROWMODES,active=CCCCCCCC")
    _feed(synchronizer, "FDCISO,sd=low,verified=1,restartRequired=0")
    _feed(synchronizer, "ABAT,mv=3900,valid=1,fresh=1,reason=ok")
    _feed(synchronizer, "RAIL,valid=1,fresh=1,avdd=3391000,avss=-2500000,source=internal")
    _feed(synchronizer, "CAL,source=1,schema=3,valid=1")
    assert sent[-1] == "ST=AUTO"
    _feed(synchronizer, "ACK,cmd=ST,v=auto")
    assert sent[-1] == "BTX?"
    _feed(synchronizer, "ACK,cmd=BTX,v=FAST")
    assert synchronizer.status.state == "SYNCED"


def test_bootstrap_keeps_polling_through_realistic_firmware_boot_sweep() -> None:
    sent: list[str] = []
    synchronizer = DeviceSynchronizer(sent.append)
    synchronizer.start("serial", 4, 2)
    _feed(synchronizer, "PROTO,version=1,wires=ascii,ctrlMax=512,dataMax=1536,channels=CTRL/DATA/LOG/LIFECYCLE")
    _feed(synchronizer, "BUILD,idf=5.5.5,target=esp32s3,project=SensorArray,proto=1")
    _feed(synchronizer, "BOOT,boot=3,bootId=9,reset=usb,stage=boot_sweep,err=0x0,ready=0")
    _feed(synchronizer, "READY,ready=0,boot=3,bootId=9,stage=boot_sweep,err=0x0")

    for _ in range(25):
        synchronizer.tick(synchronizer._nextReadyPoll)
        assert sent[-1] == "READY?"
        _feed(synchronizer, "READY,ready=0,boot=3,bootId=9,stage=boot_sweep,err=0x0")

    assert synchronizer.status.state == "BOOTSTRAPPING"
    assert synchronizer.status.readyPolls == 26
    synchronizer.tick(synchronizer._nextReadyPoll)
    _feed(synchronizer, "READY,ready=1,boot=3,bootId=9,stage=ready,err=0x0")
    assert sent[-1] == "STATE?"
    assert synchronizer.status.state == "BOOTSTRAPPING"


def test_bootstrap_ready_polling_remains_bounded() -> None:
    sent: list[str] = []
    synchronizer = DeviceSynchronizer(sent.append, maximum_ready_polls=3)
    synchronizer.start("serial", 4, 2)
    _feed(synchronizer, "PROTO,version=1,wires=ascii,ctrlMax=512,dataMax=1536,channels=CTRL/DATA/LOG/LIFECYCLE")
    _feed(synchronizer, "BUILD,idf=5.5.5,target=esp32s3,project=SensorArray,proto=1")
    _feed(synchronizer, "BOOT,boot=3,bootId=9,reset=usb,stage=boot_sweep,err=0x0,ready=0")
    _feed(synchronizer, "READY,ready=0,boot=3,bootId=9,stage=boot_sweep,err=0x0")
    for _ in range(2):
        synchronizer.tick(synchronizer._nextReadyPoll)
        _feed(synchronizer, "READY,ready=0,boot=3,bootId=9,stage=boot_sweep,err=0x0")

    synchronizer.tick(synchronizer._nextReadyPoll)
    assert synchronizer.status.state == "FAILED"
    assert synchronizer.status.error == "READY remained false after bounded polling"


def test_fdc_parser_exposes_strict_query_apply_and_restart_required_states():
    protocol = TextLogProtocol()
    query = protocol.feed_line("FDCISO,sd=low,verified=1,restartRequired=0", _envelope())
    queried = next(event for event in query if isinstance(event, FdcIsolationInfo))
    assert (queried.sd, queried.verified, queried.restartRequired, queried.state) == (
        "low", True, False, "snapshot"
    )

    applied = next(
        event
        for event in protocol.feed_line(
            "FAPP,id=8,seq=50,sd=high,verified=1,restartRequired=1,state=applied", _envelope()
        )
        if isinstance(event, FdcIsolationInfo)
    )
    assert (applied.sd, applied.verified, applied.restartRequired, applied.requestId) == (
        "high", True, True, 8
    )

    rejected = next(
        event
        for event in protocol.feed_line(
            "FERR,cmd=FDCISO,sd=high,state=rejected,reason=restart_required,restartRequired=1",
            _envelope(),
        )
        if isinstance(event, FdcIsolationInfo)
    )
    assert rejected.error == "restart_required"
    assert rejected.restartRequired is True


def test_backend_fdc_guards_match_firmware_homogeneous_ads_contract():
    runtime = BackendRuntime(AppConfiguration())
    sent: list[str] = []
    runtime.transport.send_command = sent.append  # type: ignore[method-assign]
    runtime.commands.authoritativeStateKnown = True
    runtime.commands.appliedMode = "RES"
    runtime.commands.appliedRowModes = ("RES",) * 8
    runtime.deviceInfo["fdcIsolation"] = {
        "sd": "low", "verified": True, "restartRequired": False, "state": "snapshot"
    }

    response = runtime.request_fdc_isolation(True)
    assert response["requested"] == "ON"
    assert sent == ["FDCISO=ON"]

    runtime.commands.appliedRowModes = ("RES", "VOLT") + ("RES",) * 6
    with pytest.raises(ValueError, match="homogeneous VOLT or RES"):
        runtime.request_fdc_isolation(True)

    runtime.commands.appliedMode = "CAP"
    runtime.commands.appliedRowModes = ("CAP",) * 8
    with pytest.raises(ValueError, match="homogeneous VOLT or RES"):
        runtime.request_fdc_isolation(True)


def test_fdc_restart_required_blocks_cap_and_off_without_resending():
    runtime = BackendRuntime(AppConfiguration())
    sent: list[str] = []
    runtime.transport.send_command = sent.append  # type: ignore[method-assign]
    runtime.deviceInfo["fdcIsolation"] = {
        "sd": "high", "verified": True, "restartRequired": True, "state": "applied"
    }

    with pytest.raises(ValueError, match="CAP is unavailable"):
        runtime.request_measurement_mode_api("CAP")
    with pytest.raises(ValueError, match="CAP is unavailable"):
        runtime.request_row_modes_api(("RES", "CAP") + ("RES",) * 6)
    with pytest.raises(ValueError, match="Restart required"):
        runtime.request_fdc_isolation(False)
    assert sent == []


def test_new_boot_invalidates_old_boot_scoped_fdc_usb_calibration_and_performance():
    runtime = BackendRuntime(AppConfiguration())
    runtime.commands.bootId = 10
    runtime.deviceInfo.update(
        {
            "fdcIsolation": {"sd": "high", "restartRequired": True},
            "usbStream": {"mode": "DEBUG"},
            "calibration": {"valid": True},
            "performance": {"SF50": {"physicalCaptureFps": 100}},
        }
    )
    runtime._handle_event(
        BootInfo(
            boot=2,
            bootId=11,
            reset="software",
            stage="ready",
            err="0x0",
            guard="normal",
            ready=True,
            sessionGeneration=0,
            connectionGeneration=2,
        )
    )
    assert runtime.deviceInfo["fdcIsolation"] is None
    assert runtime.deviceInfo["usbStream"] is None
    assert runtime.deviceInfo["calibration"] is None
    assert runtime.deviceInfo["performance"] == {}
    assert runtime.deviceInfo["lifecycleEvents"][-1]["kind"] == "DEVICE_REBOOT"
