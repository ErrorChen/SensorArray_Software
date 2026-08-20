from sensorarray_app.domain.lifecycle import classify_reset_reason


def test_reset_reason_classification_preserves_power_and_watchdog_semantics():
    brownout = classify_reset_reason("brownout")
    assert brownout == {
        "raw": "brownout",
        "category": "brownout",
        "label": "Power-related device reset (brownout)",
        "powerRelated": True,
        "severity": "error",
    }
    assert classify_reset_reason("power_glitch")["powerRelated"] is True
    assert classify_reset_reason("task_wdt")["category"] == "task_watchdog"
    assert classify_reset_reason("int_wdt")["category"] == "interrupt_watchdog"
    assert classify_reset_reason("panic")["category"] == "panic"


def test_expected_restart_refines_only_software_reset_and_recovery_safe_wins():
    assert classify_reset_reason(
        "software", expected_restart=True, expected_command="restart"
    )["category"] == "manual_restart"
    assert classify_reset_reason(
        "software", expected_restart=True, expected_command="recover"
    )["category"] == "recovery_restart"
    assert classify_reset_reason(
        "brownout", expected_restart=True, expected_command="restart"
    )["category"] == "brownout"
    assert classify_reset_reason(
        "usb", prev_stage="manual_restart", expected_restart=True, expected_command="restart"
    )["category"] == "manual_restart"
    assert classify_reset_reason(
        "usb", prev_stage="auto_restart", expected_restart=True, expected_command="recover"
    )["category"] == "recovery_restart"
    assert classify_reset_reason(
        "usb", expected_restart=True, expected_command="restart"
    )["category"] == "usb_reset"
    assert classify_reset_reason(
        "brownout", prev_stage="manual_restart", expected_restart=True, expected_command="restart"
    )["category"] == "brownout"
    assert classify_reset_reason(
        "software", guard="recovery_safe", expected_restart=True, expected_command="recover"
    )["category"] == "recovery_safe_boot"


def test_unknown_reset_tokens_remain_visible():
    classified = classify_reset_reason("future_reason")
    assert classified["raw"] == "future_reason"
    assert classified["category"] == "unknown"
    assert "future_reason" in classified["label"]
