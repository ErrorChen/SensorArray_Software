from sensorarray_app.store.statistics_store import StatisticsStore


def test_serial_debug_decimation_is_not_reported_as_packet_loss():
    stats = StatisticsStore()
    for seq in (1, 51, 101):
        stats.record_frame(
            seq=seq,
            source="serial",
            boot_id=7,
            connection_generation=1,
            usb_mode="DEBUG",
            data_every=50,
        )
    payload = stats.snapshot()
    assert payload["observedSequenceGapFrames"] == 98
    assert payload["intentionalFirmwareDecimation"] == 98
    assert payload["unknownSequenceGap"] == 0


def test_missing_debug_output_full_ble_and_replay_gaps_remain_explicit():
    debug = StatisticsStore()
    debug.record_frame(seq=1, source="serial", boot_id=8, usb_mode="DEBUG", data_every=50)
    debug.record_frame(seq=101, source="serial", boot_id=8, usb_mode="DEBUG", data_every=50)
    assert debug.intentionalFirmwareDecimation == 98
    assert debug.unknownSequenceGap == 1

    for source, mode in (("serial", "FULL"), ("ble", None), ("replay", None)):
        stats = StatisticsStore()
        stats.record_frame(seq=10, source=source, boot_id=9, usb_mode=mode, data_every=50)
        stats.record_frame(seq=13, source=source, boot_id=9, usb_mode=mode, data_every=50)
        assert stats.intentionalFirmwareDecimation == 0
        assert stats.unknownSequenceGap == 2


def test_new_boot_resets_sequence_identity_and_drop_reports_are_deltas():
    stats = StatisticsStore()
    stats.record_frame(seq=100, source="serial", boot_id=1, usb_mode="FULL", data_every=1)
    stats.record_frame(seq=1, source="serial", boot_id=2, usb_mode="FULL", data_every=1)
    assert stats.unknownSequenceGap == 0

    stats.record_firmware_drop_report("serial:0", 3)
    stats.record_firmware_drop_report("serial:0", 5)
    stats.record_firmware_drop_report("serial:0", 4)
    stats.record_host_transport_drop(2)
    assert stats.firmwareTransportDrop == 5
    assert stats.hostTransportDrop == 2


def test_link_reconnect_starts_new_receive_sequence_baseline_for_same_boot():
    stats = StatisticsStore()
    stats.record_frame(seq=100, source="ble", boot_id=7, connection_generation=1)
    stats.begin_connection_epoch(
        source="ble",
        boot_id=7,
        connection_generation=2,
        reconnect=True,
    )

    # The device legitimately continued publishing while the BLE link was
    # down.  That interval is a transport discontinuity, not Host packet loss.
    stats.record_frame(seq=250, source="ble", boot_id=7, connection_generation=2)
    assert stats.unknownSequenceGap == 0
    assert stats.reconnectCount == 1

    # Loss inside the new physical connection epoch remains strict.
    stats.record_frame(seq=253, source="ble", boot_id=7, connection_generation=2)
    assert stats.unknownSequenceGap == 2


def test_cumulative_drop_report_uses_first_attach_value_only_as_baseline() -> None:
    stats = StatisticsStore()
    stats.record_firmware_drop_report("ble:4:data", 19, baseline_first=True)
    assert stats.firmwareTransportDrop == 0
    stats.record_firmware_drop_report("ble:4:data", 22, baseline_first=True)
    assert stats.firmwareTransportDrop == 3


def test_sf50_nonfresh_window_reports_aggregate_drop_without_misattributing_it():
    stats = StatisticsStore()
    stats.record_frame(seq=101, source="ble", boot_id=4)
    stats.record_frame(seq=105, source="ble", boot_id=4)
    assert stats.unknownSequenceGap == 3

    stats.record_firmware_output_window(
        source="ble",
        boot_id=4,
        connection_generation=1,
        sequence_start=101,
        sequence_end=110,
        frame_count=10,
        invalid_frames=2,
        firmware_drops=1,
    )
    payload = stats.snapshot()
    assert payload["firmwareSuppressedNonFresh"] == 2
    assert payload["firmwareReportedDrop"] == 1
    assert payload["firmwareAttributedSequenceGap"] == 0
    assert payload["hostUnexplainedSequenceGap"] == 1
    assert payload["rejectsByReason"]["unknown_sequence_gap"] == 1


def test_late_sf50_nonfresh_evidence_precedes_source_specific_drop_attribution() -> None:
    stats = StatisticsStore()
    stats.record_frame(seq=1, source="serial", boot_id=15, usb_mode="FULL", data_every=1)
    stats.record_frame(seq=7, source="serial", boot_id=15, usb_mode="FULL", data_every=1)
    stats.record_firmware_output_window(
        source="serial",
        boot_id=15,
        connection_generation=1,
        sequence_start=1,
        sequence_end=7,
        frame_count=7,
        invalid_frames=1,
        firmware_drops=3,
    )
    assert stats.firmwareSuppressedNonFresh == 1
    assert stats.firmwareAttributedSequenceGap == 0
    assert stats.unknownSequenceGap == 4

    # SF50 is cumulative and may grow from its 15-frame partial report.  Its
    # additional non-fresh evidence must get first claim on the sequence gap.
    stats.record_firmware_output_window(
        source="serial",
        boot_id=15,
        connection_generation=1,
        sequence_start=1,
        sequence_end=7,
        frame_count=7,
        invalid_frames=3,
        firmware_drops=3,
    )
    assert stats.firmwareSuppressedNonFresh == 3
    assert stats.unknownSequenceGap == 2

    stats.record_firmware_drop_report(
        "serial:15:usbDrop",
        3,
        source="serial",
        boot_id=15,
        connection_generation=1,
        attribute_sequence=True,
    )
    assert stats.firmwareAttributedSequenceGap == 2
    assert stats.unknownSequenceGap == 0


def test_perf_usb_drop_reconciles_only_current_serial_sequence_interval() -> None:
    stats = StatisticsStore()
    stats.record_frame(seq=100, source="serial", boot_id=12, usb_mode="FULL", data_every=1)
    stats.record_firmware_drop_report(
        "serial:12:usbDrop",
        4,
        source="serial",
        boot_id=12,
        connection_generation=1,
        attribute_sequence=True,
    )
    stats.record_frame(seq=105, source="serial", boot_id=12, usb_mode="FULL", data_every=1)
    assert stats.unknownSequenceGap == 4

    stats.record_firmware_drop_report(
        "serial:12:usbDrop",
        8,
        source="serial",
        boot_id=12,
        connection_generation=1,
        attribute_sequence=True,
    )
    payload = stats.snapshot()
    assert payload["firmwareReportedDrop"] == 8
    assert payload["firmwareAttributedSequenceGap"] == 4
    assert payload["hostUnexplainedSequenceGap"] == 0


def test_sf50_and_perf_overlap_does_not_double_count_firmware_drop_total() -> None:
    stats = StatisticsStore()
    stats.record_firmware_output_window(
        source="serial",
        boot_id=13,
        connection_generation=1,
        sequence_start=1,
        sequence_end=50,
        frame_count=50,
        invalid_frames=0,
        firmware_drops=3,
    )
    stats.record_firmware_drop_report("serial:13:usbDrop", 3)
    assert stats.snapshot()["firmwareReportedDrop"] == 3


def test_overlapping_drop_reports_cannot_attribute_more_gaps_than_reported() -> None:
    stats = StatisticsStore()
    stats.record_frame(seq=1, source="serial", boot_id=14, usb_mode="FULL", data_every=1)
    stats.record_frame(seq=7, source="serial", boot_id=14, usb_mode="FULL", data_every=1)
    stats.record_firmware_output_window(
        source="serial",
        boot_id=14,
        connection_generation=1,
        sequence_start=1,
        sequence_end=7,
        frame_count=7,
        invalid_frames=0,
        firmware_drops=3,
    )
    stats.record_firmware_drop_report(
        "serial:14:usbDrop",
        3,
        source="serial",
        boot_id=14,
        connection_generation=1,
        attribute_sequence=True,
    )
    payload = stats.snapshot()
    assert payload["firmwareReportedDrop"] == 3
    assert payload["firmwareAttributedSequenceGap"] == 3
    assert payload["hostUnexplainedSequenceGap"] == 2


def test_partial_sf50_reports_are_cumulative_and_can_arrive_before_gap():
    stats = StatisticsStore()
    stats.record_frame(seq=100, source="ble", boot_id=5)
    for end, count, invalid in ((115, 15, 1), (130, 30, 2), (145, 45, 3)):
        stats.record_firmware_output_window(
            source="ble",
            boot_id=5,
            connection_generation=1,
            sequence_start=101,
            sequence_end=end,
            frame_count=count,
            invalid_frames=invalid,
        )
    stats.record_frame(seq=104, source="ble", boot_id=5)
    assert stats.firmwareSuppressedNonFresh == 3
    assert stats.unknownSequenceGap == 0


def test_sf50_reconciles_only_the_matching_transport_boot_and_sequence_window():
    stats = StatisticsStore()
    stats.record_frame(seq=10, source="ble", boot_id=6)
    stats.record_frame(seq=13, source="ble", boot_id=6)
    stats.record_firmware_output_window(
        source="serial",
        boot_id=6,
        connection_generation=1,
        sequence_start=10,
        sequence_end=13,
        frame_count=4,
        invalid_frames=2,
    )
    stats.record_firmware_output_window(
        source="ble",
        boot_id=7,
        connection_generation=1,
        sequence_start=10,
        sequence_end=13,
        frame_count=4,
        invalid_frames=2,
    )
    assert stats.unknownSequenceGap == 2


def test_consecutive_perf_snapshots_close_a_trailing_nonfresh_gap():
    stats = StatisticsStore()
    stats.record_frame(seq=200, source="ble", boot_id=8)
    stats.record_firmware_performance_counters(
        source="ble",
        boot_id=8,
        connection_generation=1,
        published_frames=1000,
        fresh_frames=990,
    )
    stats.record_frame(seq=203, source="ble", boot_id=8)
    stats.record_firmware_performance_counters(
        source="ble",
        boot_id=8,
        connection_generation=1,
        published_frames=1003,
        fresh_frames=991,
    )
    assert stats.firmwareSuppressedNonFresh == 2
    assert stats.unknownSequenceGap == 0

    stats.record_frame(seq=205, source="ble", boot_id=8)
    stats.record_firmware_performance_counters(
        source="ble",
        boot_id=8,
        connection_generation=1,
        published_frames=1005,
        fresh_frames=992,
    )
    assert stats.firmwareSuppressedNonFresh == 3
    assert stats.unknownSequenceGap == 0


def test_perf_does_not_reuse_nonfresh_frames_already_attributed_by_sf50():
    stats = StatisticsStore()
    stats.record_frame(seq=300, source="ble", boot_id=9)
    stats.record_firmware_performance_counters(
        source="ble",
        boot_id=9,
        connection_generation=1,
        published_frames=2000,
        fresh_frames=1990,
    )
    stats.record_frame(seq=303, source="ble", boot_id=9)
    stats.record_firmware_output_window(
        source="ble",
        boot_id=9,
        connection_generation=1,
        sequence_start=301,
        sequence_end=303,
        frame_count=3,
        invalid_frames=1,
    )
    assert stats.unknownSequenceGap == 1

    stats.record_firmware_performance_counters(
        source="ble",
        boot_id=9,
        connection_generation=1,
        published_frames=2003,
        fresh_frames=1992,
    )
    assert stats.firmwareSuppressedNonFresh == 1
    assert stats.unknownSequenceGap == 1


def test_perf_reply_causal_tail_is_pending_until_a_later_watermark_closes_it():
    stats = StatisticsStore()
    stats.record_frame(seq=100, source="serial", boot_id=16, usb_mode="FULL", data_every=1)
    stats.record_firmware_performance_counters(
        source="serial",
        boot_id=16,
        connection_generation=1,
        published_frames=100,
        fresh_frames=100,
        sequence_end=100,
    )

    # PERF was captured at sequence 100 but queued behind the live stream. By
    # the time Host receives the reply, sequence 102 has exposed missing 101.
    stats.record_frame(seq=102, source="serial", boot_id=16, usb_mode="FULL", data_every=1)
    stats.record_firmware_performance_counters(
        source="serial",
        boot_id=16,
        connection_generation=1,
        published_frames=100,
        fresh_frames=100,
        sequence_end=100,
    )
    pending = stats.snapshot()
    assert pending["unknownSequenceGap"] == 1
    assert pending["pendingFirmwareEvidenceGap"] == 1
    assert pending["hostUnexplainedSequenceGap"] == 0

    # The next causal watermark includes the missing physical frame and proves
    # that it was a firmware-suppressed non-fresh publication, not Host loss.
    stats.record_firmware_performance_counters(
        source="serial",
        boot_id=16,
        connection_generation=1,
        published_frames=102,
        fresh_frames=101,
        sequence_end=102,
    )
    closed = stats.snapshot()
    assert closed["unknownSequenceGap"] == 0
    assert closed["pendingFirmwareEvidenceGap"] == 0
    assert closed["hostUnexplainedSequenceGap"] == 0


def test_debug_full_policy_change_starts_a_new_sequence_epoch():
    stats = StatisticsStore()
    stats.record_frame(seq=50, source="serial", boot_id=10, usb_mode="DEBUG", data_every=50)
    stats.record_frame(seq=100, source="serial", boot_id=10, usb_mode="DEBUG", data_every=50)
    assert stats.intentionalFirmwareDecimation == 49
    stats.begin_output_policy(source="serial", boot_id=10, connection_generation=1)
    stats.record_frame(seq=137, source="serial", boot_id=10, usb_mode="FULL", data_every=1)
    stats.record_frame(seq=138, source="serial", boot_id=10, usb_mode="FULL", data_every=1)
    assert stats.hostTransportDrop == 0
    assert stats.unknownSequenceGap == 0
