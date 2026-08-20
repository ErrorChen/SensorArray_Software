from __future__ import annotations

from typing import Any


def classify_reset_reason(
    reset_reason: str | None,
    *,
    guard: str | None = None,
    prev_stage: str | None = None,
    expected_restart: bool = False,
    expected_command: str | None = None,
) -> dict[str, Any]:
    """Map the exact firmware reset token to stable host/UI semantics.

    The raw firmware token remains authoritative and is returned unchanged;
    these fields are presentation/audit classifications only.  An expected
    RESTART/RECOVER transaction plus its retained firmware breadcrumb can
    refine a software/USB reset, but never hides a brownout, watchdog, panic,
    or other unexpected hardware reason.
    """

    raw = str(reset_reason or "unknown").strip().lower() or "unknown"
    guard_name = str(guard or "").strip().lower()
    previous_stage = str(prev_stage or "").strip().lower()
    expected_kind = str(expected_command or "").strip().lower()

    critical_reset_reasons = {"brownout", "power_glitch", "task_wdt", "int_wdt", "wdt", "panic", "cpu_lockup"}
    if raw in critical_reset_reasons:
        return _mapped_classification(raw)
    if guard_name == "recovery_safe":
        return _classification(raw, "recovery_safe_boot", "Recovery-safe boot", False, "error")
    manual_restart_evidence = raw == "software" or (
        raw in {"usb", "jtag"} and previous_stage == "manual_restart"
    )
    recovery_restart_evidence = raw == "software" or (
        raw in {"usb", "jtag"} and previous_stage == "auto_restart"
    )
    if expected_restart and manual_restart_evidence and expected_kind == "restart":
        return _classification(raw, "manual_restart", "Manual restart", False, "info")
    if expected_restart and recovery_restart_evidence and expected_kind == "recover":
        return _classification(raw, "recovery_restart", "Recovery restart", False, "warning")

    return _mapped_classification(raw)


def _mapped_classification(raw: str) -> dict[str, Any]:

    mapping: dict[str, tuple[str, str, bool, str]] = {
        "poweron": ("power_on", "Power-on reset", True, "info"),
        "external": ("external_reset", "External reset", False, "warning"),
        "software": ("software_reset", "Software reset", False, "info"),
        "task_wdt": ("task_watchdog", "Task watchdog reset", False, "error"),
        "int_wdt": ("interrupt_watchdog", "Interrupt watchdog reset", False, "error"),
        "wdt": ("watchdog", "Watchdog reset", False, "error"),
        "brownout": ("brownout", "Power-related device reset (brownout)", True, "error"),
        "power_glitch": ("power_glitch", "Power-related device reset (power glitch)", True, "error"),
        "panic": ("panic", "Panic reset", False, "error"),
        "cpu_lockup": ("cpu_lockup", "CPU lockup reset", False, "error"),
        "deepsleep": ("deep_sleep", "Deep-sleep wake reset", False, "info"),
        "usb": ("usb_reset", "USB reset", False, "warning"),
        "jtag": ("jtag_reset", "JTAG reset", False, "warning"),
        "sdio": ("sdio_reset", "SDIO reset", False, "warning"),
        "efuse": ("efuse_reset", "eFuse reset", False, "warning"),
        "unknown": ("unknown", "Unknown reset", False, "warning"),
    }
    category, label, power_related, severity = mapping.get(
        raw,
        ("unknown", f"Unknown reset ({raw})", False, "warning"),
    )
    return _classification(raw, category, label, power_related, severity)


def _classification(raw: str, category: str, label: str, power_related: bool, severity: str) -> dict[str, Any]:
    return {
        "raw": raw,
        "category": category,
        "label": label,
        "powerRelated": bool(power_related),
        "severity": severity,
    }


__all__ = ["classify_reset_reason"]
