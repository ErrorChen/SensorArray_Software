from __future__ import annotations

import time

from sensorarray_app.domain.models import (
    AdsDiagnosticEvent,
    BatteryTelemetry,
    CommandAccepted,
    CommandApplied,
    CommandTransactionEvent,
    LogRecord,
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
