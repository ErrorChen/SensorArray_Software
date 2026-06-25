from __future__ import annotations

import asyncio
import queue
import threading
import time
from collections.abc import Iterable
from concurrent.futures import TimeoutError as FutureTimeoutError

from sensorarray_app.constants import BLE_CTRL_RX_UUID, BLE_CTRL_TX_UUID, BLE_DATA_TX_UUID, BLE_LOG_TX_UUID
from sensorarray_app.domain.models import TransportEnvelope, TransportStateEvent
from sensorarray_app.protocol.ble_fragments import BleFragmentReassembler, normalize_ble_channel


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
        self._notify_map: dict[str, str] = {}
        self._ctrl_rx_uuid: str | None = None
        self._ctrl_write_response = True
        self._stop_requested = threading.Event()
        self._fragmenter = BleFragmentReassembler()
        self._notify_counts = {"data": 0, "log": 0, "ctrl": 0}
        self._notify_bytes = {"data": 0, "log": 0, "ctrl": 0}
        self._notify_failures = {"data": 0, "log": 0, "ctrl": 0}
        self._last_payload_prefix = ""
        self._start_time = time.monotonic()
        self._silent_data_warning_emitted = False

    def start(self) -> None:
        self._stop_requested.clear()
        self._start_time = time.monotonic()
        self._thread = threading.Thread(target=self._run_loop, name="SensorArrayBleTransport", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_requested.set()
        if self._loop is not None:
            self._loop.call_soon_threadsafe(lambda: None)
        if self._thread is not None:
            self._thread.join(timeout=4.0)

    def send_command(self, command: str) -> None:
        self.write((command.rstrip() + "\n").encode("ascii", errors="strict"))

    def write(self, data: bytes) -> int:
        if self._loop is None or self._client is None:
            raise RuntimeError("BLE is not connected")
        payload = bytes(data)
        future = asyncio.run_coroutine_threadsafe(self._write_gatt(payload), self._loop)
        try:
            return int(future.result(timeout=3.0))
        except FutureTimeoutError as exc:
            raise RuntimeError("BLE write timed out") from exc

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._connect())
        finally:
            pending = [task for task in asyncio.all_tasks(self._loop) if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()
            self._loop = None

    async def _connect(self) -> None:
        notify_map: dict[str, str] = {}
        try:
            from bleak import BleakClient

            self._put_state("CONNECTING", self.address)
            client = BleakClient(self.address)
            self._client = client
            await client.connect()
            self._put_state("CONNECTED", self.address)
            services = getattr(client, "services", None)
            if services is None and hasattr(client, "get_services"):
                services = await client.get_services()
            notify_map = self._resolve_notify_characteristics(services)
            self._notify_map = notify_map
            self._resolve_ctrl_characteristic(services)
            self._put_state("GATT", f"notify={notify_map},ctrl={self._ctrl_rx_uuid or 'none'}")
            for channel, uuid in notify_map.items():
                await client.start_notify(uuid, lambda _, data, ch=channel: self._notify(ch, bytes(data)))
            self._put_state("STREAMING", self.address)
            while getattr(client, "is_connected", False) and not self._stop_requested.is_set():
                await asyncio.sleep(0.2)
        except Exception as exc:
            self._put_state("ERROR", str(exc))
        finally:
            client = self._client
            if client is not None:
                for uuid in notify_map.values():
                    try:
                        await client.stop_notify(uuid)
                    except Exception:
                        pass
                try:
                    if getattr(client, "is_connected", False):
                        await client.disconnect()
                except Exception as exc:
                    self._put_state("ERROR", f"disconnect: {exc}")
            self._client = None
            self._notify_map = {}
            self._ctrl_rx_uuid = None
            self._put_state("DISCONNECTED", "stopped" if self._stop_requested.is_set() else "")

    async def _write_gatt(self, payload: bytes) -> int:
        client = self._client
        if client is None or not getattr(client, "is_connected", False):
            raise RuntimeError("BLE is not connected")
        if not self._ctrl_rx_uuid:
            raise NotImplementedError("BLE ctrl characteristic is not available")
        await client.write_gatt_char(self._ctrl_rx_uuid, payload, response=self._ctrl_write_response)
        return len(payload)

    def _notify(self, channel: str, data: bytes) -> None:
        now_ns = time.monotonic_ns()
        normalized_channel = normalize_ble_channel(channel)
        self._record_notify(normalized_channel, data)
        for out_channel, payload in self._fragmenter.feed(normalized_channel, data, now_ns):
            envelope = TransportEnvelope(
                source="ble",
                channel=out_channel,
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
        self._maybe_emit_diagnostics(now_ns)

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

    def _resolve_ctrl_characteristic(self, services) -> None:
        self._ctrl_rx_uuid = None
        self._ctrl_write_response = True
        for service in services:
            for char in service.characteristics:
                uuid = str(char.uuid)
                if _match_uuid(BLE_CTRL_RX_UUID, [uuid]) is None:
                    continue
                props = {str(item).lower() for item in getattr(char, "properties", [])}
                if "write" in props or "write-without-response" in props:
                    self._ctrl_rx_uuid = uuid
                    self._ctrl_write_response = "write" in props
                    return

    def _record_notify(self, channel: str, data: bytes) -> None:
        key = channel if channel in self._notify_counts else "log"
        self._notify_counts[key] += 1
        self._notify_bytes[key] += len(data)
        self._last_payload_prefix = f"{channel}:{_payload_prefix(data)}"

    def _maybe_emit_diagnostics(self, now_ns: int) -> None:
        total = sum(self._notify_counts.values())
        if total and total % 50 == 0:
            stats = self._fragmenter.stats
            self._put_log_envelope(
                now_ns,
                (
                    "BLE_RX50,"
                    f"data={self._notify_counts['data']}/{self._notify_bytes['data']}/"
                    f"{stats.reassembled}/{self._notify_failures['data']},"
                    f"log={self._notify_counts['log']}/{self._notify_bytes['log']}/{stats.reassembled}/"
                    f"{self._notify_failures['log']},"
                    f"ctrl={self._notify_counts['ctrl']}/{self._notify_bytes['ctrl']}/{stats.reassembled}/"
                    f"{self._notify_failures['ctrl']},"
                    f"last={self._last_payload_prefix},state=streaming"
                ),
            )
            self._put_log_envelope(
                now_ns,
                (
                    "BLE_FRAG50,"
                    f"rx={stats.received},reasm={stats.reassembled},dup={stats.duplicate},"
                    f"miss={stats.missing},to={stats.timeout},crc={stats.crcFailure},"
                    f"len={stats.lengthFailure},unknown={stats.unknownChannel}"
                ),
            )
        if (
            not self._silent_data_warning_emitted
            and time.monotonic() - self._start_time > 3.0
            and self._notify_counts["data"] == 0
            and self._notify_counts["log"] > 0
        ):
            self._silent_data_warning_emitted = True
            self._put_log_envelope(
                now_ns,
                (
                    "WARN,BLE data notify is silent while log notify is active; firmware may be sending data "
                    "through log characteristic or host channel mapping is wrong"
                ),
            )

    def _put_log_envelope(self, now_ns: int, text: str) -> None:
        envelope = TransportEnvelope(
            source="ble",
            channel="log",
            deviceId=self.deviceId,
            sessionGeneration=self.sessionGeneration,
            receivedMonotonicNs=now_ns,
            receivedWallTime=time.time(),
            rawPayload=(text + "\n").encode("ascii", errors="replace"),
        )
        try:
            self.outputQueue.put_nowait(envelope)
        except queue.Full:
            pass


def _match_uuid(expected: str, values: Iterable[str]) -> str | None:
    expected_lower = expected.lower()
    short = expected_lower[4:8] if expected_lower.startswith("0000") else expected_lower
    for value in values:
        lower = value.lower()
        if lower == expected_lower or lower.startswith(f"0000{short}-"):
            return value
    return None


def _payload_prefix(data: bytes, limit: int = 48) -> str:
    safe = data[:limit].replace(b"\r", b" ").replace(b"\n", b" ")
    try:
        text = safe.decode("ascii", errors="strict")
    except UnicodeDecodeError:
        text = safe.hex(" ")
    return text
