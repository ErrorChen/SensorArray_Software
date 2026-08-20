from __future__ import annotations

from sensorarray_app.app.configuration import AppConfiguration
from sensorarray_app.domain.models import BootInfo, CommandTransactionEvent, RestartEvent
from sensorarray_backend.core.runtime import BackendRuntime


def _boot(boot_id: int, reset: str = "software", *, guard: str = "normal") -> BootInfo:
    return BootInfo(
        boot=boot_id,
        bootId=boot_id,
        reset=reset,
        stage="ready",
        err="0x0",
        seq=3,
        heap=250_000,
        heapMin=220_000,
        prevStage="streaming",
        prevErr="0x0",
        prevHeap=230_000,
        guard=guard,
        autoRestarts=0,
        ready=True,
        sessionGeneration=0,
        connectionGeneration=1,
    )


def test_scenario_a_same_boot_after_link_reconnect_is_not_device_reboot():
    runtime = BackendRuntime(AppConfiguration())
    runtime.commands.begin_connection(1)
    runtime._handle_event(_boot(101, "poweron"))
    assert runtime.deviceInfo["lifecycleEvents"][-1]["kind"] == "FIRST_ATTACH"

    runtime.commands.begin_connection(2)
    runtime._handle_event(_boot(101, "poweron"))
    lifecycle = runtime.deviceInfo["lifecycleEvents"][-1]
    assert lifecycle["kind"] == "TRANSPORT_RECONNECT"
    assert lifecycle["oldBootId"] == lifecycle["newBootId"] == 101


def test_scenario_b_new_boot_aborts_pending_transaction_and_resets_frame_epoch():
    runtime = BackendRuntime(AppConfiguration())
    runtime.commands.begin_connection(1)
    runtime._handle_event(_boot(101, "poweron"))
    runtime.commands.request_mode("RES", lambda _command: None)
    runtime.commands.begin_connection(2)
    assert runtime.commands.transitionState == "outcome_unknown"

    runtime._handle_event(_boot(102, "software"))
    lifecycle = runtime.deviceInfo["lifecycleEvents"][-1]
    assert lifecycle["kind"] == "DEVICE_REBOOT"
    assert (lifecycle["oldBootId"], lifecycle["newBootId"]) == (101, 102)
    assert runtime.commands.pendingMode is None
    assert runtime.commands.snapshot()["latestCommand"]["state"] == "ABORTED_BY_REBOOT"
    assert runtime.matrixStore.resyncRequired is True


def test_scenario_c_host_attach_reconstructs_running_res_mode_without_forcing_cap():
    runtime = BackendRuntime(AppConfiguration())
    runtime.commands.begin_connection(1)
    runtime._handle_event(_boot(77, "poweron"))
    runtime.commands.resync_authoritative(mode="RES", rows=3, row_modes=("RES",) * 8)
    measurement = runtime.commands.measurement_snapshot()
    assert measurement["appliedMode"] == "RES"
    assert measurement["rowProfile"]["appliedModes"] == ["RES"] * 8
    assert runtime.commands.activeRows == 3
    assert measurement["authoritativeStateKnown"] is True


def test_scenario_d_expected_restart_completes_only_after_new_boot():
    runtime = BackendRuntime(AppConfiguration())
    runtime.commands.begin_connection(1)
    runtime._handle_event(_boot(10, "poweron"))
    runtime.commands.record_action("restart", "RESTART", None, lambda _command: None)
    runtime.commands.handle(
        CommandTransactionEvent(
            commandType="restart",
            phase="accepted",
            requestId=42,
            state="accepted",
            sessionGeneration=0,
            rawText="RACK,id=42,state=accepted",
        )
    )
    runtime.commands.mark_expected_restart(42, command_type="restart")
    assert runtime.commands.snapshot()["latestCommand"]["state"] == "EXPECTED_RESTART"

    runtime._handle_event(_boot(11, "software"))
    lifecycle = runtime.deviceInfo["lifecycleEvents"][-1]
    assert lifecycle["kind"] == "DEVICE_REBOOT"
    assert lifecycle["expected"] is True
    assert lifecycle["resetCategory"] == "manual_restart"
    assert runtime.commands.snapshot()["latestCommand"]["state"] == "COMPLETED_AFTER_REBOOT"
    assert runtime.commands.expectedRestart is False


def test_expected_restart_is_written_as_an_explicit_session_discontinuity():
    runtime = BackendRuntime(AppConfiguration())
    runtime.commands.bootId = 10
    runtime.transport.status["connectionGeneration"] = 3
    events: list[tuple[str, dict]] = []
    runtime.recorder.record_event = lambda kind, details=None: events.append(  # type: ignore[method-assign,assignment]
        (kind, dict(details or {}))
    )

    runtime._handle_event(RestartEvent(phase="restarting", requestId=42, kind="manual"))

    assert events == [
        (
            "EXPECTED_RESTART",
            {
                "requestId": 42,
                "commandKind": "manual",
                "oldBootId": 10,
                "connectionGeneration": 3,
            },
        )
    ]


def test_scenarios_e_and_f_surface_watchdog_and_brownout_as_unexpected():
    for reason, category, power_related in (
        ("task_wdt", "task_watchdog", False),
        ("brownout", "brownout", True),
    ):
        runtime = BackendRuntime(AppConfiguration())
        runtime.commands.begin_connection(1)
        runtime._handle_event(_boot(1, "poweron"))
        runtime._handle_event(_boot(2, reason))
        lifecycle = runtime.deviceInfo["lifecycleEvents"][-1]
        assert lifecycle["kind"] == "DEVICE_REBOOT"
        assert lifecycle["expected"] is False
        assert lifecycle["resetCategory"] == category
        assert lifecycle["powerRelated"] is power_related
        assert lifecycle["resetSeverity"] == "error"


def test_same_boot_resync_resolves_ambiguous_write_instead_of_replaying_command():
    runtime = BackendRuntime(AppConfiguration())
    sent: list[str] = []
    runtime.commands.request_mode("VOLT", sent.append)
    runtime.commands.begin_connection(2)
    assert sent == ["MODE=VOLT"]
    assert runtime.commands.snapshot()["latestCommand"]["state"] == "OUTCOME_UNKNOWN"

    result = runtime.commands.resync_authoritative(mode="VOLT", rows=8, row_modes=("VOLT",) * 8)
    assert result["modeOutcome"] == "RESYNC_CONFIRMED_APPLIED"
    assert sent == ["MODE=VOLT"]
    assert runtime.commands.snapshot()["latestCommand"]["state"] == "RESYNC_CONFIRMED_APPLIED"
