from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from sensorarray_app.constants import DEFAULT_SERIAL_BAUD
from sensorarray_backend.api.dependencies import get_runtime
from sensorarray_backend.core.runtime import BackendRuntime

router = APIRouter(prefix="/api/transport")


class ModeRequest(BaseModel):
    mode: str


class SerialConnectRequest(BaseModel):
    port: str = Field(min_length=1)
    baud: int = DEFAULT_SERIAL_BAUD
    autoReconnect: bool = True


class BleConnectRequest(BaseModel):
    address: str = Field(min_length=1)
    deviceId: str = ""
    autoReconnect: bool = True


class WifiConnectRequest(BaseModel):
    host: str = Field(min_length=1)


class TransportWriteRequest(BaseModel):
    text: str = ""
    lineEnding: str = "lf"
    encoding: str = "utf-8"
    mode: str = "text"
    hex: str | None = None


@router.post("/mode")
def set_mode(body: ModeRequest, runtime: BackendRuntime = Depends(get_runtime)) -> dict:
    try:
        return runtime.set_transport_mode(body.mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/serial/ports")
def serial_ports(runtime: BackendRuntime = Depends(get_runtime)) -> dict:
    try:
        return {"ok": True, "ports": runtime.list_serial_ports(), "error": ""}
    except Exception as exc:
        return {"ok": False, "ports": [], "error": str(exc)}


@router.post("/serial/connect")
def serial_connect(body: SerialConnectRequest, runtime: BackendRuntime = Depends(get_runtime)) -> dict:
    try:
        runtime.connect_serial(body.port, body.baud, body.autoReconnect)
        return {"ok": True, "mode": "serial", "port": body.port, "baud": body.baud}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/ble/scan")
async def ble_scan(timeout: float = 10.0, runtime: BackendRuntime = Depends(get_runtime)) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        devices = await asyncio.to_thread(runtime.scan_ble_once, timeout)
        state = runtime.discovery_payload()["bleState"]
        error = state.removeprefix("failed: ").strip() if state.startswith("failed: ") else ""
        return {
            "ok": not error,
            "devices": devices,
            "advancedDevices": [device for device in devices if device.get("advanced")],
            "error": error,
            "state": state,
            "durationMs": int((time.perf_counter() - started) * 1000),
        }
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        state = runtime.discovery_payload()["bleState"]
        error = str(exc)
        return {
            "ok": False,
            "devices": [],
            "advancedDevices": [],
            "error": error,
            "state": state if state.startswith("failed: ") else f"failed: {error}",
            "durationMs": int((time.perf_counter() - started) * 1000),
        }


@router.post("/ble/connect")
def ble_connect(body: BleConnectRequest, runtime: BackendRuntime = Depends(get_runtime)) -> dict:
    try:
        runtime.connect_ble(body.address, body.deviceId, body.autoReconnect)
        return {"ok": True, "mode": "ble", "address": body.address}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/wifi/discover")
async def wifi_discover(subnet: str | None = None, runtime: BackendRuntime = Depends(get_runtime)) -> dict[str, Any]:
    try:
        devices = await asyncio.to_thread(runtime.scan_wifi_once, subnet)
        return {"devices": devices, "state": runtime.discovery_payload()["wifiState"]}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/wifi/connect")
def wifi_connect(body: WifiConnectRequest, runtime: BackendRuntime = Depends(get_runtime)) -> dict:
    try:
        runtime.connect_wifi(body.host)
        return {"ok": True, "mode": "wifi", "host": body.host}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/disconnect")
def disconnect(runtime: BackendRuntime = Depends(get_runtime)) -> dict:
    runtime.disconnect()
    return {"ok": True}


@router.post("/write")
def write_transport(body: TransportWriteRequest, runtime: BackendRuntime = Depends(get_runtime)) -> dict[str, Any]:
    return runtime.write_to_active_transport(
        text=body.text,
        line_ending=body.lineEnding,
        encoding=body.encoding,
        mode=body.mode,
        hex_text=body.hex,
    )
