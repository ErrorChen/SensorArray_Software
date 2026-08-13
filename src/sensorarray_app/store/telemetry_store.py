from __future__ import annotations

from dataclasses import asdict
from typing import Any

from sensorarray_app.domain.models import BatteryTelemetry, RailTelemetry


class TelemetryStore:
    """Typed latest-attempt and last-known-good device telemetry.

    A failed battery attempt is important diagnostic state, but it must not
    erase the last voltage measured successfully. The cache is keyed by a
    stable transport/device identity rather than React component lifetime or a
    transient connection generation.
    """

    def __init__(self):
        self.latestBatteryAttempt: BatteryTelemetry | None = None
        self.lastGoodBattery: dict[str, Any] | None = None
        self.railTelemetry: RailTelemetry | None = None
        self.deviceIdentity = ""
        self._batteryConnectionStale = False
        self._railConnectionStale = False
        self.revision = 0

    @property
    def battery(self) -> BatteryTelemetry | None:
        """Compatibility alias for pre-latest/lastGood consumers."""

        return self.latestBatteryAttempt

    def begin_device(self, identity: str) -> None:
        normalized = str(identity or "").strip()
        if not normalized:
            self._batteryConnectionStale = True
            self._railConnectionStale = True
            self.revision += 1
            return
        if self.deviceIdentity and normalized != self.deviceIdentity:
            self.latestBatteryAttempt = None
            self.lastGoodBattery = None
            self.railTelemetry = None
        self.deviceIdentity = normalized
        self._batteryConnectionStale = True
        self._railConnectionStale = True
        self.revision += 1

    def mark_connection_stale(self) -> None:
        self._batteryConnectionStale = True
        self._railConnectionStale = True
        self.revision += 1

    def update_battery(self, telemetry: BatteryTelemetry) -> None:
        self.latestBatteryAttempt = telemetry
        firmware_last_good_mv = getattr(telemetry, "lastGoodBatteryMv", None)
        firmware_last_good_valid = getattr(telemetry, "lastGoodValid", None)
        firmware_has_last_good = any(
            key in telemetry.rawFields
            for key in (
                "lastGoodMv",
                "lastGoodValid",
                "lastGoodFresh",
                "lastGoodAgeMs",
                "lastGoodFrame",
                "bl",
                "blValid",
                "blFresh",
                "blAgeMs",
                "blFrame",
                "blSource",
                "blReason",
            )
        )
        if firmware_has_last_good:
            if firmware_last_good_mv is not None and firmware_last_good_valid is not False:
                self.lastGoodBattery = {
                    "batteryMv": int(firmware_last_good_mv),
                    "receivedTime": float(telemetry.receivedTime),
                    "ageMs": getattr(telemetry, "lastGoodAgeMs", None),
                    "fresh": getattr(telemetry, "lastGoodFresh", None),
                    "frame": getattr(telemetry, "lastGoodFrame", None),
                    "source": getattr(telemetry, "lastGoodSource", None) or "firmware",
                    "reason": getattr(telemetry, "lastGoodReason", None) or "ok",
                    "firmwareAuthoritative": True,
                }
            else:
                # An explicit firmware invalid state overrides host history.
                # Session fallback is used only when the new bl* fields are
                # absent (old firmware compatibility).
                self.lastGoodBattery = None
        elif telemetry.batteryMv is not None and telemetry.valid is not False:
            self.lastGoodBattery = {
                "batteryMv": int(telemetry.batteryMv),
                "receivedTime": float(telemetry.receivedTime),
                "ageMs": telemetry.ageMs,
                "source": "host_session_fallback",
                "reason": telemetry.reason or "ok",
                "firmwareAuthoritative": False,
            }
        if telemetry.valid is not False and telemetry.batteryMv is not None and telemetry.fresh is not False:
            self._batteryConnectionStale = False
        self.revision += 1

    def update_rail(self, telemetry: RailTelemetry) -> None:
        self.railTelemetry = telemetry
        if getattr(telemetry, "fresh", None) is True:
            self._railConnectionStale = False
        self.revision += 1

    def reset(self) -> None:
        self.latestBatteryAttempt = None
        self.lastGoodBattery = None
        self.railTelemetry = None
        self.deviceIdentity = ""
        self._batteryConnectionStale = False
        self._railConnectionStale = False
        self.revision += 1

    def battery_snapshot(self, now: float) -> dict[str, Any]:
        latest = self.latestBatteryAttempt
        latest_payload = self._battery_attempt_payload(latest, now) if latest is not None else None
        last_good = dict(self.lastGoodBattery) if self.lastGoodBattery is not None else None
        if last_good is not None:
            received_age = max(0.0, float(now) - float(last_good["receivedTime"]))
            firmware_age = last_good.get("ageMs")
            age_seconds = received_age + (max(0, int(firmware_age)) / 1000.0 if firmware_age is not None else 0.0)
            last_good.update(
                {
                    "ageSeconds": age_seconds,
                    "batteryText": f"{int(last_good['batteryMv']) / 1000.0:.3f} V",
                }
            )
        if last_good is None:
            return {
                # Preserve the legacy flat battery diagnostics for existing
                # API/replay consumers.  The typed latestAttempt remains the
                # canonical attempt record, and the fields below override its
                # display state so an invalid attempt cannot masquerade as a
                # usable last-known value.
                **(latest_payload or {}),
                "revision": self.revision,
                "available": False,
                "state": "Unknown",
                "batteryText": "N/A",
                "batteryMv": None,
                "ageSeconds": None,
                "fresh": False,
                "valid": False if latest is not None and latest.valid is False else None,
                "reason": latest.reason if latest is not None else "never_measured",
                "latestAttempt": latest_payload,
                "lastGood": None,
                "deviceIdentity": self.deviceIdentity,
            }

        latest_is_fresh = bool(
            latest is not None
            and latest.batteryMv is not None
            and latest.valid is not False
            and latest.fresh is not False
            and not self._batteryConnectionStale
        )
        latest_failed = latest is not None and (latest.valid is False or latest.batteryMv is None)
        state = "fresh" if latest_is_fresh else "failed" if latest_failed else "stale"
        if self._batteryConnectionStale and not latest_failed:
            reason = "connection_stale"
        else:
            reason = latest.reason if latest is not None and latest.reason else ("stale" if not latest_is_fresh else "ok")
            if not latest_is_fresh and not latest_failed and str(reason).lower() in {"", "ok", "fresh"}:
                reason = "stale"
        return {
            # Keep retry/spread/restore counters available at their historical
            # top-level paths while also exposing the complete typed attempt.
            # Authoritative last-good display fields are applied afterwards.
            **(latest_payload or {}),
            "revision": self.revision,
            "available": True,
            "state": state,
            "batteryText": last_good["batteryText"],
            "batteryMv": int(last_good["batteryMv"]),
            "ageSeconds": last_good["ageSeconds"],
            "fresh": latest_is_fresh,
            "valid": False if latest_failed else True,
            "reason": reason,
            "lastKnown": not latest_is_fresh,
            "latestAttempt": latest_payload,
            "lastGood": last_good,
            "deviceIdentity": self.deviceIdentity,
        }

    def rail_snapshot(self, now: float) -> dict[str, Any]:
        telemetry = self.railTelemetry
        if telemetry is None:
            return {
                "railSpanUv": None,
                "valid": False,
                "fresh": False,
                "age": None,
                "ageSeconds": None,
                "source": "internal_monitor",
                "reason": "unavailable",
                "timestamp": None,
            }
        payload = asdict(telemetry)
        timestamp = float(payload.get("timestamp") or now)
        age_ms = payload.get("ageMs")
        age_seconds = max(0.0, now - timestamp) + (max(0, int(age_ms)) / 1000.0 if age_ms is not None else 0.0)
        if self._railConnectionStale:
            payload["fresh"] = False
            if str(payload.get("reason") or "").lower() in {"", "ok", "fresh"}:
                payload["reason"] = "connection_stale"
        payload.update({"ageSeconds": age_seconds, "age": age_seconds})
        return payload

    @staticmethod
    def _battery_attempt_payload(telemetry: BatteryTelemetry, now: float) -> dict[str, Any]:
        payload = asdict(telemetry)
        payload["ageSeconds"] = max(0.0, float(now) - float(telemetry.receivedTime))
        payload["batteryText"] = (
            "N/A" if telemetry.batteryMv is None else f"{int(telemetry.batteryMv) / 1000.0:.3f} V"
        )
        return payload
