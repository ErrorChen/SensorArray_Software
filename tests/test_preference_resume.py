from __future__ import annotations

from sensorarray_app.app.configuration import AppConfiguration
from sensorarray_app.domain.models import CommandAccepted, CommandApplied, CommandTransactionEvent, UsbStreamInfo
from sensorarray_backend.core.runtime import BackendRuntime


def _synced_runtime() -> tuple[BackendRuntime, list[str]]:
    runtime = BackendRuntime(AppConfiguration())
    sent: list[str] = []
    runtime.transport.status.update({"transport": "serial", "state": "STREAMING", "connectionGeneration": 2})
    runtime.transport.send_command = sent.append  # type: ignore[method-assign]
    runtime.synchronizer.status.state = "SYNCED"
    runtime.synchronizer.status.source = "serial"
    runtime.commands.bootId = 20
    runtime.commands.resync_authoritative(mode="CAP", rows=8, row_modes=("CAP",) * 8)
    return runtime, sent


def test_device_reboot_resume_is_strictly_sequential_and_waits_for_each_terminal():
    runtime, sent = _synced_runtime()
    runtime.resumeMeasurementAfterDeviceRestart = True
    runtime.preferredRows = 3
    runtime.preferredMeasurementMode = "RES"
    runtime.preferredRowModes = ("RES",) * 8
    runtime.preferredUsbStream = "FULL"
    runtime.lastBootTransition = {"bootChanged": True, "firstAttach": False, "oldBootId": 19, "newBootId": 20}

    runtime._on_bootstrap_complete(runtime.synchronizer.status)
    assert sent == ["ROWS=3"]
    assert runtime.lifecycle_settings_payload()["preferenceApply"]["state"] == "WAITING_ROWS"

    runtime._handle_event(
        CommandAccepted(
            commandId=31,
            oldRows=8,
            requestedRows=3,
            generation=4,
            sessionGeneration=0,
            rawText="RCMD,id=31,old=8,new=3,state=accepted",
        )
    )
    assert sent == ["ROWS=3"]
    runtime._handle_event(
        CommandApplied(
            commandId=31,
            seq=100,
            oldRows=8,
            newRows=3,
            generation=5,
            sessionGeneration=0,
            rawText="RAPP,id=31,old=8,new=3,gen=5,seq=100,state=applied",
        )
    )
    assert sent == ["ROWS=3", "ROWMODES=RRRRRRRR"]

    runtime._handle_event(
        CommandTransactionEvent(
            commandType="row_modes",
            phase="accepted",
            requestId=32,
            oldValue=("CAP",) * 8,
            requestedValue=("RES",) * 8,
            sessionGeneration=0,
        )
    )
    assert sent == ["ROWS=3", "ROWMODES=RRRRRRRR"]
    runtime._handle_event(
        CommandTransactionEvent(
            commandType="row_modes",
            phase="applied",
            requestId=32,
            requestedValue=("RES",) * 8,
            appliedValue=("RES",) * 8,
            generation=6,
            frameSeq=101,
            sessionGeneration=0,
        )
    )
    assert sent == ["ROWS=3", "ROWMODES=RRRRRRRR", "USBSTREAM=FULL"]
    assert runtime.lifecycle_settings_payload()["preferenceApply"]["state"] == "WAITING_USB_STREAM"

    runtime._handle_event(UsbStreamInfo(mode="FULL", dataEvery=1, diagEvery=15, state="applied"))
    status = runtime.lifecycle_settings_payload()["preferenceApply"]
    assert status["state"] == "COMPLETE"
    assert status["commands"] == ["ROWS=3", "ROWMODES=RRRRRRRR", "USBSTREAM=FULL"]
    assert sent == status["commands"]


def test_resume_defaults_off_and_first_attach_never_overwrites_running_device_state():
    runtime, sent = _synced_runtime()
    runtime.preferredRows = 1
    runtime.preferredRowModes = ("RES",) * 8
    runtime.lastBootTransition = {"bootChanged": True, "firstAttach": False, "oldBootId": 19, "newBootId": 20}
    runtime._on_bootstrap_complete(runtime.synchronizer.status)
    assert sent == []
    assert runtime.lifecycle_settings_payload()["preferenceApply"]["state"] == "DISABLED"

    runtime.resumeMeasurementAfterDeviceRestart = True
    runtime.lastBootTransition = {"bootChanged": False, "firstAttach": True, "oldBootId": None, "newBootId": 20}
    runtime._preferenceApplyState["state"] = "IDLE"
    runtime._on_bootstrap_complete(runtime.synchronizer.status)
    assert sent == []


def test_explicit_profile_during_bootstrap_is_stored_without_command_storm():
    runtime = BackendRuntime(AppConfiguration())
    sent: list[str] = []
    runtime.transport.status.update({"transport": "serial", "state": "STREAMING"})
    runtime.transport.send_command = sent.append  # type: ignore[method-assign]
    runtime.synchronizer.status.state = "BOOTSTRAPPING"

    result = runtime.apply_setup_profile(
        {
            "schemaVersion": 3,
            "acquisition": {"rows": 4, "measurementMode": "VOLT", "rowModes": ["VOLT"] * 8},
            "lifecycle": {
                "autoReconnect": True,
                "resumeMeasurementAfterDeviceRestart": True,
                "preferredUsbStream": "DEBUG",
            },
        }
    )
    assert sent == []
    assert runtime.preferredRows == 4
    assert runtime.preferredRowModes == ("VOLT",) * 8
    assert result["profile"]["lifecycle"]["preferredUsbStream"] == "DEBUG"
    assert result["warnings"] == ["measurement preferences stored and will apply in order after bootstrap"]
    assert runtime.lifecycle_settings_payload()["preferenceApply"]["state"] == "WAITING_BOOTSTRAP"


def test_lifecycle_settings_reject_unknown_usb_preference():
    runtime = BackendRuntime(AppConfiguration())
    try:
        runtime.update_lifecycle_settings({"preferredUsbStream": "TURBO"})
    except ValueError as exc:
        assert "DEVICE_DEFAULT" in str(exc)
    else:
        raise AssertionError("unknown USB stream preference was accepted")

