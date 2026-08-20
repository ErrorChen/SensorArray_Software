from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from sensorarray_backend.api.dependencies import get_runtime
from sensorarray_backend.core.runtime import BackendRuntime


router = APIRouter(prefix="/api")


class UsbStreamRequest(BaseModel):
    mode: str


class FdcIsolationRequest(BaseModel):
    enabled: bool


class RecoverRequest(BaseModel):
    level: int | None = Field(default=None, ge=0, le=2)


class RecordingStartRequest(BaseModel):
    directory: str = Field(min_length=1)
    allowReducedStream: bool = False


@router.get("/device/protocol")
def device_protocol(runtime: BackendRuntime = Depends(get_runtime)) -> dict[str, Any]:
    return {"ok": True, "protocol": runtime.deviceInfo.get("protocol")}


@router.get("/device/build")
def device_build(runtime: BackendRuntime = Depends(get_runtime)) -> dict[str, Any]:
    return {"ok": True, "build": runtime.deviceInfo.get("build")}


@router.get("/device/boot")
def device_boot(runtime: BackendRuntime = Depends(get_runtime)) -> dict[str, Any]:
    return {"ok": True, "boot": runtime.deviceInfo.get("boot"), "events": runtime.deviceInfo.get("lifecycleEvents", [])}


@router.get("/device/ready")
def device_ready(runtime: BackendRuntime = Depends(get_runtime)) -> dict[str, Any]:
    return {"ok": True, "ready": runtime.deviceInfo.get("ready")}


@router.get("/device/state")
def device_state(runtime: BackendRuntime = Depends(get_runtime)) -> dict[str, Any]:
    return runtime.device_status_payload()


@router.get("/device/performance")
def device_performance(runtime: BackendRuntime = Depends(get_runtime)) -> dict[str, Any]:
    return {"ok": True, "performance": runtime.deviceInfo.get("performance", {})}


@router.post("/device/restart")
def restart_device(runtime: BackendRuntime = Depends(get_runtime)) -> dict[str, Any]:
    return _call(runtime.request_restart)


@router.post("/device/recover")
def recover_device(body: RecoverRequest, runtime: BackendRuntime = Depends(get_runtime)) -> dict[str, Any]:
    return _call(runtime.request_recover, body.level)


@router.get("/transport/usb-stream")
def usb_stream(runtime: BackendRuntime = Depends(get_runtime)) -> dict[str, Any]:
    return {"ok": True, "usbStream": runtime.deviceInfo.get("usbStream")}


@router.post("/transport/usb-stream")
def set_usb_stream(body: UsbStreamRequest, runtime: BackendRuntime = Depends(get_runtime)) -> dict[str, Any]:
    return _call(runtime.request_usb_stream, body.mode)


@router.get("/diagnostics/fdc-isolation")
def fdc_isolation(runtime: BackendRuntime = Depends(get_runtime)) -> dict[str, Any]:
    return {"ok": True, "fdcIsolation": runtime.deviceInfo.get("fdcIsolation")}


@router.post("/diagnostics/fdc-isolation")
def set_fdc_isolation(body: FdcIsolationRequest, runtime: BackendRuntime = Depends(get_runtime)) -> dict[str, Any]:
    return _call(runtime.request_fdc_isolation, body.enabled)


@router.get("/calibration")
def calibration(runtime: BackendRuntime = Depends(get_runtime)) -> dict[str, Any]:
    return {"ok": True, "calibration": runtime.deviceInfo.get("calibration")}


@router.post("/calibration/save")
def calibration_save(runtime: BackendRuntime = Depends(get_runtime)) -> dict[str, Any]:
    # The frontend must present the destructive persistent-write confirmation;
    # the backend still uses the typed ACK -> CALSV transaction path.
    return _call(runtime.request_calibration, "SAVE")


@router.post("/calibration/load")
def calibration_load(runtime: BackendRuntime = Depends(get_runtime)) -> dict[str, Any]:
    return _call(runtime.request_calibration, "LOAD")


@router.get("/battery")
def battery(runtime: BackendRuntime = Depends(get_runtime)) -> dict[str, Any]:
    return {"ok": True, "battery": runtime.telemetry.battery_snapshot(time.time())}


@router.get("/rail")
def rail(runtime: BackendRuntime = Depends(get_runtime)) -> dict[str, Any]:
    return {"ok": True, "rail": runtime.telemetry.rail_snapshot(time.time())}


@router.get("/recording")
def recording(runtime: BackendRuntime = Depends(get_runtime)) -> dict[str, Any]:
    return {"ok": True, "recording": runtime.recorder.snapshot()}


@router.post("/recording/start")
def recording_start(body: RecordingStartRequest, runtime: BackendRuntime = Depends(get_runtime)) -> dict[str, Any]:
    return _call(runtime.start_scientific_recording, body.directory, allow_reduced_stream=body.allowReducedStream)


@router.post("/recording/stop")
def recording_stop(runtime: BackendRuntime = Depends(get_runtime)) -> dict[str, Any]:
    return _call(runtime.stop_scientific_recording)


def _call(function, *args, **kwargs) -> dict[str, Any]:
    try:
        return function(*args, **kwargs)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
