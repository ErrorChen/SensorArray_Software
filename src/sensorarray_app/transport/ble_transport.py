from __future__ import annotations

import asyncio
import queue
import threading
import time
from collections.abc import Iterable

from sensorarray_app.constants import BLE_CTRL_RX_UUID, BLE_CTRL_TX_UUID, BLE_DATA_TX_UUID, BLE_LOG_TX_UUID
from sensorarray_app.domain.models import TransportEnvelope, TransportStateEvent
from sensorarray_app.protocol.ble_fragments import BleFragmentReassembler


class BleTransport:
    source = "ble"

    def __init__(self, output_queue: "queue.Queue", session_generation: int, address: str, device_id: str = ""):
        self.outputQueue = output_queue
        self.sessionGeneration = int(session_generation)
        self.address = address
        self.deviceId = device_id or address
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client = None
        self._fragmenter = BleFragmentReassembler()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run_loop, name="SensorArrayBleTransport", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=4.0)

    def send_command(self, command: str) -> None:
        if self._loop is None or self._client is None:
            raise RuntimeError("BLE is not connected")
        payload = (command.rstrip() + "\n").encode("ascii", errors="strict")
        asyncio.run_coroutine_threadsafe(self._client.write_gatt_char(BLE_CTRL_RX_UUID, payload), self._loop)

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.create_task(self._connect())
        self._loop.run_forever()

    async def _connect(self) -> None:
        try:
            from bleak import BleakClient

            self._put_state("CONNECTING", self.address)
            async with BleakClient(self.address) as client:
                self._client = client
                self._put_state("CONNECTED", self.address)
                notify_map = self._resolve_notify_characteristics(client.services)
                self._put_state("GATT", f"notify={notify_map}")
                for channel, uuid in notify_map.items():
                    await client.start_notify(uuid, lambda _, data, ch=channel: self._notify(ch, bytes(data)))
                self._put_state("STREAMING", self.address)
                while client.is_connected:
                    await asyncio.sleep(0.5)
        except Exception as exc:
            self._put_state("ERROR", str(exc))
        finally:
            self._client = None
            self._put_state("DISCONNECTED", "")

    def _notify(self, channel: str, data: bytes) -> None:
        now_ns = time.monotonic_ns()
        for out_channel, payload in self._fragmenter.feed(channel, data, now_ns):
            envelope = TransportEnvelope(
                source="ble",
                channel=out_channel,  # type: ignore[arg-type]
                deviceId=self.deviceId,
                sessionGeneration=self.sessionGeneration,
                receivedMonotonicNs=now_ns,
                receivedWallTime=time.time(),
                rawPayload=payload,
            )
            try:
                self.outputQueue.put_nowait(envelope)
            except queue.Full:
                pass

    def _put_state(self, state: str, message: str) -> None:
        try:
            self.outputQueue.put_nowait(TransportStateEvent("ble", state, self.sessionGeneration, message))
        except queue.Full:
            pass

    def _resolve_notify_characteristics(self, services) -> dict[str, str]:
        notify_chars: list[tuple[str, set[str]]] = []
        for service in services:
            for char in service.characteristics:
                props = {str(item).lower() for item in getattr(char, "properties", [])}
                if "notify" in props or "indicate" in props:
                    notify_chars.append((char.uuid, props))
        all_notify = [uuid for uuid, _props in notify_chars]
        mapping: dict[str, str] = {}
        for channel, expected in (("data", BLE_DATA_TX_UUID), ("log", BLE_LOG_TX_UUID), ("ctrl", BLE_CTRL_TX_UUID)):
            match = _match_uuid(expected, all_notify)
            if match:
                mapping[channel] = match
        if "data" not in mapping and all_notify:
            mapping["data"] = all_notify[0]
        if not mapping:
            raise RuntimeError("no notify or indicate BLE characteristics found")
        return mapping


def _match_uuid(expected: str, values: Iterable[str]) -> str | None:
    expected_lower = expected.lower()
    short = expected_lower[4:8] if expected_lower.startswith("0000") else expected_lower
    for value in values:
        lower = value.lower()
        if lower == expected_lower or lower.startswith(f"0000{short}-"):
            return value
    return None
