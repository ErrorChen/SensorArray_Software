from __future__ import annotations

import time

from sensorarray_app.domain.models import (
    AdsDiagnosticEvent,
    BatteryTelemetry,
    CalibrationInfo,
    CommandAccepted,
    CommandApplied,
    CommandTransactionEvent,
    LogRecord,
    PerformanceInfo,
    RailTelemetry,
    RecoveryEvent,
    RestartEvent,
    TransportEnvelope,
)
from sensorarray_app.protocol.log_protocol import TextLogProtocol


def envelope() -> TransportEnvelope:
    return TransportEnvelope(
        source="serial",
        channel="log",
        deviceId="COM_TEST",
        sessionGeneration=4,
        receivedMonotonicNs=time.monotonic_ns(),
        receivedWallTime=1234.5,
        rawPayload=b"",
    )


def transactions(events: list[object]) -> list[CommandTransactionEvent]:
    return [event for event in events if isinstance(event, CommandTransactionEvent)]


def ads_events(events: list[object]) -> list[AdsDiagnosticEvent]:
    return [event for event in events if isinstance(event, AdsDiagnosticEvent)]


def calibration_info(events: list[object]) -> CalibrationInfo:
    return next(event for event in events if isinstance(event, CalibrationInfo))


def test_calibration_query_and_save_load_terminals_preserve_authoritative_metadata():
    protocol = TextLogProtocol()
    queried = calibration_info(
        protocol.feed_line(
            "CAL,source=0,schema=0,valid=0,boardId=00000000,hardwareRev=0,payloadLength=0",
            envelope(),
        )
    )
    assert queried.valid is False
    assert queried.source == "0"
    assert queried.boardId == "00000000"

    accepted = transactions(protocol.feed_line("ACK,cmd=CAL,v=SAVE,status=accepted", envelope()))[0]
    assert (accepted.commandType, accepted.phase, accepted.requestedValue) == (
        "calibration_save",
        "accepted",
        "SAVE",
    )

    saved_events = protocol.feed_line(
        "CALSV,state=saved,reason=persisted,source=1,schema=3,valid=1,boardId=12345678,hardwareRev=2,payloadLength=512,err=0x0",
        envelope(),
    )
    saved = transactions(saved_events)[0]
    saved_info = calibration_info(saved_events)
    assert (saved.commandType, saved.phase) == ("calibration_save", "complete")
    assert (saved_info.valid, saved_info.schema, saved_info.hardwareRev, saved_info.payloadLength) == (
        True,
        3,
        2,
        512,
    )

    default_events = protocol.feed_line(
        "CALLD,state=default,reason=no_blob,source=0,schema=0,valid=0,boardId=00000000,hardwareRev=0,payloadLength=0,err=0x1102",
        envelope(),
    )
    loaded = transactions(default_events)[0]
    default_info = calibration_info(default_events)
    assert (loaded.commandType, loaded.phase) == ("calibration_load", "failed")
    assert (default_info.state, default_info.valid, default_info.rawFields["reason"]) == (
        "default",
        False,
        "no_blob",
    )


def test_mode_ack_apply_and_error_are_generic_correlated_transactions():
    protocol = TextLogProtocol()
    accepted = transactions(protocol.feed_line("MACK,id=42,old=CAP,new=VOLT,state=accepted", envelope()))[0]
    assert accepted.commandType == "mode"
    assert accepted.phase == "accepted"
    assert accepted.requestId == 42
    assert accepted.oldValue == "CAP"
    assert accepted.requestedValue == "VOLT"
    assert accepted.appliedValue is None

    applied = transactions(
        protocol.feed_line(
            "MAPP,id=42,gen=7,old=CAP,new=VOLT,seq=8,state=applied,transitionUs=500",
            envelope(),
        )
    )[0]
    assert applied.phase == "applied"
    assert applied.requestId == 42
    assert applied.appliedValue == "VOLT"
    assert (applied.generation, applied.frameSeq) == (7, 8)
    assert applied.rawFields["transitionUs"] == "500"

    failed = transactions(
        protocol.feed_line("MERR,id=43,old=CAP,new=VOLT,seq=9,state=SAFE,err=0x103", envelope())
    )[0]
    assert failed.commandType == "mode"
    assert failed.phase == "failed"
    assert failed.error == "0x103"


def test_row_mode_ack_apply_and_error_are_typed_atomic_transactions():
    protocol = TextLogProtocol()
    accepted = transactions(
        protocol.feed_line(
            "RMACK,id=62,old=CCCCCCCC,new=RVVCCVVR,state=accepted",
            envelope(),
        )
    )[0]
    assert (accepted.commandType, accepted.phase, accepted.requestId) == ("row_modes", "accepted", 62)
    assert accepted.oldValue == ("CAP",) * 8
    assert accepted.requestedValue == ("RES", "VOLT", "VOLT", "CAP", "CAP", "VOLT", "VOLT", "RES")
    assert accepted.appliedValue is None

    applied = transactions(
        protocol.feed_line(
            "RMAPP,id=62,gen=11,seq=201,profile=RVVCCVVR,state=applied",
            envelope(),
        )
    )[0]
    assert (applied.commandType, applied.phase, applied.requestId) == ("row_modes", "applied", 62)
    assert (applied.generation, applied.frameSeq) == (11, 201)
    assert applied.appliedValue == accepted.requestedValue

    failed = transactions(
        protocol.feed_line(
            "RMERR,id=63,gen=12,seq=202,profile=CRVCRVCR,err=0x108,state=rejected,route=SAFE",
            envelope(),
        )
    )[0]
    assert (failed.commandType, failed.phase, failed.requestId) == ("row_modes", "failed", 63)
    assert failed.requestedValue == ("CAP", "RES", "VOLT", "CAP", "RES", "VOLT", "CAP", "RES")
    assert (failed.generation, failed.frameSeq) == (12, 202)
    assert failed.error == "0x108"


def test_malformed_row_mode_profile_stays_observable_but_cannot_advance_typed_state():
    events = TextLogProtocol().feed_line(
        "RMAPP,id=62,gen=11,seq=201,new=RVV,state=applied",
        envelope(),
    )
    assert len([event for event in events if isinstance(event, LogRecord)]) == 1
    assert not transactions(events)


def test_rows_legacy_events_are_retained_alongside_generic_transactions():
    protocol = TextLogProtocol()
    acceptedEvents = protocol.feed_line("RCMD,id=4,old=8,req=2,generation=6,status=accepted", envelope())
    legacyAccepted = [event for event in acceptedEvents if isinstance(event, CommandAccepted)]
    genericAccepted = transactions(acceptedEvents)
    assert len(legacyAccepted) == len(genericAccepted) == 1
    assert legacyAccepted[0].requestedRows == 2
    assert genericAccepted[0].commandType == "rows"
    assert genericAccepted[0].requestedValue == 2

    appliedEvents = protocol.feed_line("RAPP,id=4,seq=10,old=8,new=2,gen=7,status=applied", envelope())
    legacyApplied = [event for event in appliedEvents if isinstance(event, CommandApplied)]
    genericApplied = transactions(appliedEvents)
    assert len(legacyApplied) == len(genericApplied) == 1
    assert legacyApplied[0].newRows == 2
    assert genericApplied[0].appliedValue == 2


def test_rail_rapp_is_disambiguated_from_rows_and_tracks_two_phase_state():
    protocol = TextLogProtocol()
    rack = transactions(
        protocol.feed_line(
            "RACK,id=51,avdd=3391000,avss=-2500000,source=external,state=accepted",
            envelope(),
        )
    )[0]
    assert rack.commandType == "rail"
    assert rack.phase == "accepted"
    assert rack.requestedValue == {"avddUv": 3_391_000, "avssUv": -2_500_000, "source": "external"}

    appliedEvents = protocol.feed_line(
        "RAPP,id=51,gen=3,seq=9,avdd=3391000,avss=-2500000,source=external,state=applied",
        envelope(),
    )
    assert not any(isinstance(event, CommandApplied) for event in appliedEvents)
    rapp = transactions(appliedEvents)[0]
    assert (rapp.commandType, rapp.phase, rapp.generation, rapp.frameSeq) == ("rail", "applied", 3, 9)
    assert rapp.appliedValue["avssUv"] == -2_500_000

    rejected = transactions(
        protocol.feed_line(
            "RERR,id=52,seq=10,avdd=3391000,avss=-2500000,err=0x102,state=rejected",
            envelope(),
        )
    )[0]
    assert rejected.commandType == "rail"
    assert rejected.phase == "rejected"
    assert rejected.error == "0x102"


def test_rail_recover_and_restart_deferred_transactions_never_cross_route():
    protocol = TextLogProtocol()

    recover_ack = transactions(
        protocol.feed_line("RACK,cmd=RECOVER,id=71,level=2,state=accepted", envelope())
    )[0]
    assert (recover_ack.commandType, recover_ack.phase, recover_ack.requestedValue) == (
        "recover",
        "accepted",
        {"level": 2},
    )

    recover_events = protocol.feed_line(
        "RAPP,cmd=RECOVER,id=71,level=2,state=restarting,kind=auto", envelope()
    )
    recover_transaction = transactions(recover_events)[0]
    assert (recover_transaction.commandType, recover_transaction.phase) == ("recover", "restarting")
    assert any(isinstance(event, RecoveryEvent) and event.kind == "restarting" for event in recover_events)
    assert any(isinstance(event, RestartEvent) and event.kind == "auto" for event in recover_events)

    rejected_events = protocol.feed_line(
        "RERR,cmd=RECOVER,id=72,level=1,state=rejected,reason=fdc_restart_required,restartRequired=1",
        envelope(),
    )
    rejected = transactions(rejected_events)[0]
    assert (rejected.commandType, rejected.phase) == ("recover", "rejected")
    assert rejected.requestedValue == {"level": 1}
    assert not any(
        isinstance(event, CommandTransactionEvent) and event.commandType == "rail"
        for event in rejected_events
    )
    assert any(
        isinstance(event, RecoveryEvent)
        and event.kind == "rejected"
        and event.resetReason == "fdc_restart_required"
        for event in rejected_events
    )

    restart_ack = transactions(
        protocol.feed_line("RACK,cmd=RESTART,id=73,state=accepted", envelope())
    )[0]
    restart_events = protocol.feed_line(
        "RAPP,cmd=RESTART,id=73,state=restarting,kind=manual", envelope()
    )
    restart_transaction = transactions(restart_events)[0]
    assert (restart_ack.commandType, restart_transaction.commandType) == ("restart", "restart")
    assert restart_transaction.phase == "restarting"
    assert any(isinstance(event, RestartEvent) and event.kind == "manual" for event in restart_events)


def test_recover_safe_is_a_successful_terminal_not_a_rail_or_failure_event():
    events = TextLogProtocol().feed_line(
        "RAPP,cmd=RECOVER,id=81,level=2,state=safe,reason=restart_guard", envelope()
    )
    transaction = transactions(events)[0]
    assert (transaction.commandType, transaction.phase, transaction.error) == ("recover", "complete", None)
    recovery = next(event for event in events if isinstance(event, RecoveryEvent))
    assert recovery.kind == "safe"


def test_performance_families_are_typed_without_losing_composite_metrics():
    protocol = TextLogProtocol()
    sf_events = protocol.feed_line(
        "SF50,seq=101-150,n=50,cfps=100.25,efps=20.50,"
        "ofps=10.0/9.5/8.0,bad=2/1/3,drop=0/2/3,q=1/4",
        envelope(),
    )
    sf = next(event for event in sf_events if isinstance(event, PerformanceInfo))
    assert sf.kind == "SF50"
    assert sf.physicalCaptureFps == 100.25
    assert sf.outputFpsBySink == (10.0, 9.5, 8.0)
    assert sf.firmwareDropsBySink == (0, 2, 3)
    assert sf.firmwareDrops == 5
    assert sf.queueDepthBySink == (1, 4)
    assert (sf.sequenceStart, sf.sequenceEnd, sf.frameCount) == (101, 150, 50)
    assert (sf.staleFrames, sf.mixedFrames, sf.invalidFrames) == (2, 1, 3)
    assert sf.sourceTransport == "serial"

    ot = next(
        event
        for event in protocol.feed_line(
            "OT50,st=ALL,out=100/90,q=1/2/3,life=4/5/6", envelope()
        )
        if isinstance(event, PerformanceInfo)
    )
    assert ot.kind == "OT50"
    assert ot.metrics["q"] == (1, 2, 3)
    assert ot.metrics["st"] == "ALL"


def test_ads_unknown_identity_stays_unconfirmed_and_is_not_battery_telemetry():
    protocol = TextLogProtocol()
    events = protocol.feed_line(
        "ADS,chip=unknown,valid=0,adc1=0,adc2=0,adc=0,ref=unsynced,pwr=vbias:0,mode=dr0,gap=off",
        envelope(),
    )
    assert not any(isinstance(event, BatteryTelemetry) for event in events)
    diagnostic = ads_events(events)[0]
    assert diagnostic.eventType == "identity"
    assert diagnostic.chip == "unknown"
    assert diagnostic.identityValid is False
    assert diagnostic.state == "unconfirmed"
    identityTransaction = transactions(events)[0]
    assert identityTransaction.commandType == "ads_identity"
    assert identityTransaction.appliedValue is None
    assert identityTransaction.rawFields["chip"] == "unknown"


def test_ads_check_ack_progress_and_statistics_remain_request_id_correlated():
    protocol = TextLogProtocol()
    accepted = transactions(
        protocol.feed_line("ACK,cmd=ADSCHK,id=7,samples=100,status=accepted", envelope())
    )[0]
    assert (accepted.commandType, accepted.phase, accepted.requestId, accepted.requestedValue) == (
        "ads_check",
        "accepted",
        7,
        100,
    )

    checkEvents = protocol.feed_line(
        "ADSCHK,id=7,ok=1,chip=1262,idreg=0x03,rev=0,adc1=1,adc2=0,power=0x02,"
        "interface=0x04,mode0=0x00,mode1=0x00,mode2=0x0F,inpmux=0x01,refmux=0x24,"
        "dr=38400,filter=sinc1,chop=0,delayUs=0,pga=bypass,reference=avdd-avss,vbias=1",
        envelope(),
    )
    check = ads_events(checkEvents)[0]
    assert (check.requestId, check.chip, check.ok, check.state) == (7, "1262", True, "checking")
    checkTransaction = transactions(checkEvents)[0]
    assert (checkTransaction.commandType, checkTransaction.phase, checkTransaction.requestId) == (
        "ads_check",
        "checking",
        7,
    )

    statEvents = protocol.feed_line(
        "ADSCHKSTAT,id=7,samples=100,fresh=100,changed=99,periodMinUs=25,periodAvgUs=26,"
        "periodMaxUs=28,spi=0,timeout=0,stale=0,statusErr=0,reset=0,restore=ok,durationUs=3000",
        envelope(),
    )
    statistics = ads_events(statEvents)[0]
    assert statistics.eventType == "statistics"
    assert statistics.ok and statistics.state == "completed"
    assert (statistics.requestedSamples, statistics.freshSamples, statistics.periodAverageUs) == (100, 100, 26)
    assert transactions(statEvents)[0].phase == "complete"


def test_ads_check_failure_exposes_counts_and_restore_result():
    protocol = TextLogProtocol()
    events = protocol.feed_line(
        "ADSCHKSTAT,id=8,samples=10,fresh=8,changed=7,periodMinUs=25,periodAvgUs=30,"
        "periodMaxUs=80,spi=1,timeout=1,stale=0,statusErr=0,reset=0,restore=fail,durationUs=5000",
        envelope(),
    )
    diagnostic = ads_events(events)[0]
    assert diagnostic.state == "failed"
    assert not diagnostic.ok
    assert diagnostic.spiErrors == 1
    assert diagnostic.drdyTimeouts == 1
    assert diagnostic.restoreResult == "fail"
    transaction = transactions(events)[0]
    assert transaction.phase == "failed"
    assert "restore=fail" in str(transaction.error)


def test_battery_telemetry_preserves_current_scheduler_and_restore_fields():
    protocol = TextLogProtocol()
    events = protocol.feed_line(
        "ABAT,bt=4012,valid=1,fresh=1,ageMs=12,periodMs=1000,due=0,run=12,validRun=11,"
        "invalidRun=1,skip=2,defer=1,boundary=4,restoreFail=0,retry=0/1,unstable=1,timeout=0,"
        "spreadRaw=5,spreadMaxRaw=9,raw=123,a8d=100,ac=2005900,a8g=2006000,ratio=2/1,"
        "rail=5200000,railState=ok,vbias=1,samples=3,sampleUs=820,restore=ok,reason=ok",
        envelope(),
    )
    telemetry = [event for event in events if isinstance(event, BatteryTelemetry)][0]
    assert telemetry.batteryMv == 4012
    assert telemetry.valid and telemetry.fresh
    assert telemetry.reason == "ok"
    assert (telemetry.ageMs, telemetry.periodMs, telemetry.runCount) == (12, 1000, 12)
    assert (telemetry.validRunCount, telemetry.invalidRunCount) == (11, 1)
    assert (telemetry.retryCount, telemetry.retryLimit) == (0, 1)
    assert (telemetry.retryLastCount, telemetry.retryTotalCount) == (0, 1)
    assert (telemetry.spreadRaw, telemetry.spreadMaximumRaw) == (5, 9)
    assert (telemetry.batteryDividerNumerator, telemetry.batteryDividerDenominator) == (2, 1)
    assert telemetry.restoreResult == "ok"
    assert telemetry.rawFields["railState"] == "ok"
    assert telemetry.railState == "ok"
    assert telemetry.railValid is True


def test_battery_telemetry_preserves_firmware_authoritative_last_good_fields():
    events = TextLogProtocol().feed_line(
        "ABAT,bt=-1,valid=0,fresh=0,reason=adc_timeout,lastGoodMv=4092,lastGoodValid=1,"
        "lastGoodFresh=1,lastGoodAgeMs=1800,lastGoodFrame=88",
        envelope(),
    )
    telemetry = [event for event in events if isinstance(event, BatteryTelemetry)][0]
    assert telemetry.batteryMv is None
    assert telemetry.valid is False
    assert telemetry.lastGoodBatteryMv == 4092
    assert telemetry.lastGoodValid is True
    assert telemetry.lastGoodFresh is True
    assert telemetry.lastGoodAgeMs == 1800
    assert telemetry.lastGoodFrame == 88
    # Firmware 8045e9e9 has no last-good-specific source/reason fields; the
    # store labels their provenance as firmware after detecting lastGood*.
    assert telemetry.lastGoodSource is None
    assert telemetry.lastGoodReason is None


def test_firmware_battery_error_is_a_typed_failed_attempt_with_last_good():
    events = TextLogProtocol().feed_line(
        "BATERR,seq=89,err=0x107,reason=adc_timeout,valid=0,lastGoodMv=4088,"
        "lastGoodValid=1,sampleUs=820,restore=ok,action=report_continue",
        envelope(),
    )
    telemetry = [event for event in events if isinstance(event, BatteryTelemetry)][0]
    assert telemetry.batteryMv is None
    assert telemetry.valid is False
    assert telemetry.reason == "adc_timeout"
    assert telemetry.lastGoodBatteryMv == 4088
    assert telemetry.lastGoodValid is True


def test_internal_monitor_rail_log_becomes_read_only_typed_span_telemetry():
    events = TextLogProtocol().feed_line(
        "ARL,src=monitor,raw=123,mon=5126000,rail=5126000,rv=1,rs=ok,age=2,ref=avdd-avss",
        envelope(),
    )
    telemetry = [event for event in events if isinstance(event, RailTelemetry)][0]
    assert telemetry.railSpanUv == 5_126_000
    assert telemetry.valid is True
    assert telemetry.fresh is True
    assert telemetry.age == 2
    assert telemetry.source == "internal_monitor"
    assert telemetry.reason == "ok"
    assert telemetry.rawFields["ref"] == "avdd-avss"


def test_stale_rail_state_is_never_marked_fresh():
    events = TextLogProtocol().feed_line(
        "RAIL,source=internal_monitor,spanUv=5126000,valid=1,state=stale,ageMs=12000",
        envelope(),
    )
    telemetry = [event for event in events if isinstance(event, RailTelemetry)][0]
    assert telemetry.valid is True
    assert telemetry.fresh is False
    assert telemetry.ageMs == 12000


def test_abat_non_ok_production_rail_states_are_not_reported_valid():
    protocol = TextLogProtocol()
    for railState in ("hold", "bad"):
        events = protocol.feed_line(
            f"ABAT,bt=-1,valid=0,fresh=0,rail=0,railState={railState},reason=rail",
            envelope(),
        )
        telemetry = [event for event in events if isinstance(event, BatteryTelemetry)][0]
        assert telemetry.railState == railState
        assert telemetry.railValid is False


def test_battery_completion_and_period_events_are_generic_transactions():
    protocol = TextLogProtocol()
    completion = transactions(
        protocol.feed_line(
            "BAPP,id=8,cmd=BATNOW,seq=10,err=0x0,durationUs=820,status=complete",
            envelope(),
        )
    )[0]
    assert (completion.commandType, completion.phase, completion.requestId, completion.frameSeq) == (
        "battery_now",
        "complete",
        8,
        10,
    )
    assert completion.error is None

    accepted = transactions(
        protocol.feed_line("ACK,cmd=BATPERIOD,id=9,enabled=1,periodMs=1000,status=accepted", envelope())
    )[0]
    applied = transactions(
        protocol.feed_line("BATPERIOD,id=9,enabled=1,periodMs=1000,status=applied", envelope())
    )[0]
    assert accepted.commandType == applied.commandType == "battery_period"
    assert accepted.requestedValue == {"enabled": True, "periodMs": 1000}
    assert applied.appliedValue == {"enabled": True, "periodMs": 1000}

    snapshot = transactions(
        protocol.feed_line("BATPERIOD,enabled=1,periodMs=1000,due=0,ageMs=12", envelope())
    )[0]
    assert snapshot.phase == "snapshot"
    assert snapshot.requestId is None


def test_unknown_firmware_log_is_retained_without_structured_side_effects():
    protocol = TextLogProtocol()
    events = protocol.feed_line("FUTURETAG,newField=1", envelope())
    assert len(events) == 1
    assert isinstance(events[0], LogRecord)
    assert not events[0].recognised
    assert events[0].parsedFields == {"newField": "1"}
