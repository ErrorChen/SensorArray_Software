from __future__ import annotations

from dataclasses import asdict

from sensorarray_app.domain.models import BatteryTelemetry


class TelemetryStore:
    def __init__(self):
        self.battery: BatteryTelemetry | None = None
        self.revision = 0

    def update_battery(self, telemetry: BatteryTelemetry) -> None:
        self.battery = telemetry
        self.revision += 1

    def reset(self) -> None:
        self.battery = None
        self.revision += 1

    def battery_snapshot(self, now: float) -> dict:
        if self.battery is None:
            return {
                "revision": self.revision,
                "available": False,
                "state": "Unknown",
                "batteryText": "N/A",
                "ageSeconds": None,
            }
        data = asdict(self.battery)
        age = max(0.0, now - self.battery.receivedTime)
        battery_text = "N/A" if self.battery.batteryMv is None else f"{self.battery.batteryMv / 1000.0:.3f} V"
        data.update({"revision": self.revision, "available": True, "batteryText": battery_text, "ageSeconds": age})
        return data
