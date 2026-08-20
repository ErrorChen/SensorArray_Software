from __future__ import annotations

import asyncio
import queue
import threading
import time
from collections import deque
from collections.abc import Iterable
from concurrent.futures import TimeoutError as FutureTimeoutError

from sensorarray_app.constants import BLE_CTRL_RX_UUID, BLE_CTRL_TX_UUID, BLE_DATA_TX_UUID, BLE_LOG_TX_UUID
from sensorarray_app.domain.models import TransportEnvelope, TransportStateEvent
from sensorarray_app.protocol.ble_fragments import BleFragmentReassembler, normalize_ble_channel
from sensorarray_app.transport.base import TransportNotSent, TransportShutdownTimeout, TransportWriteOutcomeUnknown


class BleTransport:
    source = "ble"

    def __init__(
        self,
        output_queue: "queue.Queue",
        session_generation: int,
        address: str,
        device_id: str = "",
        *,
        ble_device=None,
        auto_reconnect: bool = True,
    ):
        self.outputQueue = output_queue
        self.sessionGeneration = int(session_generation)
        self.address = address
        self.deviceId = device_id or address
        self.bleDevice = ble_device
        self.autoReconnect = bool(auto_reconnect)
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client = None
        self._notify_map: dict[str, str] = {}
        self._ctrl_rx_uuid: str | None = None
        self._ctrl_write_response = True
        self._stop_requested = threading.Event()
        self._stopped = threading.Event()
        self._stopped.set()
        self.userRequestedDisconnect = False
        self.connectionGeneration = 0
        self.connectionAttemptGeneration = 0
        self.reconnectAttempt = 0
        self.reconnectBackoff = 0.0
        self._disconnect_event: asyncio.Event | None = None
        self._fragmenter = BleFragmentReassembler()
        self._priority_backlog: deque[TransportEnvelope | TransportStateEvent] = deque(maxlen=4096)
        self._priority_lock = threading.Lock()
        self.queueCounters = {
            "controlDrops": 0,
            "lifecycleDrops": 0,
            "faultDrops": 0,
            "measurementDrops": 0,
            "measurementCoalesced": 0,
            "diagnosticDrops": 0,
        }
        self._notify_counts = {"data": 0, "log": 0, "ctrl": 0}
        self._notify_bytes = {"data": 0, "log": 0, "ctrl": 0}
        self._notify_failures = {"data": 0, "log": 0, "ctrl": 0}
        self._last_payload_prefix = ""
        self._start_time = time.monotonic()
        self._silent_data_warning_emitted = False

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("BLE transport is already running")
        self._stop_requested.clear()
        self._stopped.clear()
        self.userRequestedDisconnect = False
        self._start_time = time.monotonic()
        self._thread = threading.Thread(target=self._run_loop, name="SensorArrayBleTransport", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.userRequestedDisconnect = True
        self._stop_requested.set()
        if self._loop is not None:
            event = self._disconnect_event
            if event is not None:
                self._loop.call_soon_threadsafe(event.set)
        if self._thread is not None:
            self._thread.join(timeout=8.0)
            if self._thread.is_alive() or not self._stopped.is_set():
                raise TransportShutdownTimeout("BLE worker/client did not stop within 8 seconds")
        self._thread = None

    def send_command(self, command: str) -> None:
        self.write((command.rstrip() + "\n").encode("ascii", errors="strict"))

    def write(self, data: bytes) -> int:
        if self._loop is None or self._client is None:
            raise TransportNotSent("BLE is not connected; command was not sent")
        payload = bytes(data)
        future = asyncio.run_coroutine_threadsafe(self._write_gatt(payload), self._loop)
        try:
            return int(future.result(timeout=3.0))
        except FutureTimeoutError as exc:
            # Do not cancel or retry: write_gatt_char may already have handed
            # the bytes to the OS/Bluetooth stack.
            raise TransportWriteOutcomeUnknown("BLE write timed out; firmware outcome is unknown") from exc
        except TransportNotSent:
            raise
        except NotImplementedError as exc:
            raise TransportNotSent(str(exc)) from exc
        except Exception as exc:
            raise TransportWriteOutcomeUnknown("BLE write failed after submission; firmware outcome is unknown") from exc

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._connection_loop())
        finally:
            pending = [task for task in asyncio.all_tasks(self._loop) if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()
            self._loop = None
            self._disconnect_event = None
            self._stopped.set()

    async def _connection_loop(self) -> None:
        backoffs = (0.5, 1.0, 2.0, 5.0)
        first = True
        while not self._stop_requested.is_set():
            if not first:
                if not self.autoReconnect or self.userRequestedDisconnect:
                    break
                self.reconnectAttempt += 1
                self.reconnectBackoff = backoffs[min(self.reconnectAttempt - 1, len(backoffs) - 1)]
                self._put_state("RECONNECT_WAIT", f"retry in {self.reconnectBackoff:.1f}s")
                deadline = time.monotonic() + self.reconnectBackoff
                while not self._stop_requested.is_set() and time.monotonic() < deadline:
                    self._drain_priority_backlog()
                    await asyncio.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
                if self._stop_requested.is_set():
                    break
            first = False
            self.connectionAttemptGeneration += 1
            attempt_generation = self.connectionAttemptGeneration
            disconnected_unexpectedly = await self._connect_once(attempt_generation)
            if not disconnected_unexpectedly:
                break

    async def _resolve_device(self):
        if self.bleDevice is not None and self.reconnectAttempt == 0:
            return self.bleDevice
        try:
            from bleak import BleakScanner

            self._put_state("SCANNING", self.address)
            finder = getattr(BleakScanner, "find_device_by_address", None)
            if finder is not None:
                resolved = await finder(self.address, timeout=8.0)
                if resolved is not None:
                    self.bleDevice = resolved
                    return resolved
            devices = await BleakScanner.discover(timeout=8.0)
            for device in devices:
                if str(getattr(device, "address", "")).lower() == self.address.lower():
                    self.bleDevice = device
                    return device
                if self.deviceId and str(getattr(device, "name", "")) == self.deviceId:
                    self.bleDevice = device
                    return device
        except Exception as exc:
            self._put_state("RESOLVING", f"scan fallback: {exc}")
        return self.address

    async def _connect_once(self, attempt_generation: int) -> bool:
        notify_map: dict[str, str] = {}
        client = None
        disconnected = asyncio.Event()
        self._disconnect_event = disconnected
        try:
            from bleak import BleakClient

            target = await self._resolve_device()
            self._put_state("CONNECTING", str(getattr(target, "address", self.address)))

            def disconnected_callback(_client) -> None:
                if attempt_generation != self.connectionAttemptGeneration:
                    return
                loop = self._loop
                if loop is not None:
                    loop.call_soon_threadsafe(disconnected.set)

            try:
                client = BleakClient(target, disconnected_callback=disconnected_callback)
            except TypeError:  # compatibility with small test doubles/older Bleak
                client = BleakClient(target)
                if hasattr(client, "set_disconnected_callback"):
                    client.set_disconnected_callback(disconnected_callback)
            self._client = client
            await client.connect()
            if attempt_generation != self.connectionAttemptGeneration or self._stop_requested.is_set():
                return False
            self.connectionGeneration += 1
            self.reconnectBackoff = 0.0
            self._fragmenter.reset()
            self._put_state("CONNECTED", self.address)
            services = getattr(client, "services", None)
            if services is None and hasattr(client, "get_services"):
                services = await client.get_services()
            notify_map = self._resolve_notify_characteristics(services)
            self._notify_map = notify_map
            self._resolve_ctrl_characteristic(services)
            self._put_state("GATT_DISCOVERY", f"notify={notify_map},ctrl={self._ctrl_rx_uuid or 'none'}")
            self._put_state("SUBSCRIBING", "FF11/FF20/FF30")
            for channel, uuid in notify_map.items():
                await client.start_notify(
                    uuid,
                    lambda _, data, ch=channel, generation=attempt_generation: self._notify_for_attempt(
                        generation, ch, bytes(data)
                    ),
                )
            self._put_state("STREAMING", self.address)
            while not self._stop_requested.is_set() and not disconnected.is_set():
                self._drain_priority_backlog()
                if not getattr(client, "is_connected", False):
                    disconnected.set()
                    break
                try:
                    await asyncio.wait_for(disconnected.wait(), timeout=0.2)
                except asyncio.TimeoutError:
                    pass
            return not self._stop_requested.is_set() and not self.userRequestedDisconnect
        except Exception as exc:
            self._put_state("ERROR", str(exc))
            return not self._stop_requested.is_set() and not self.userRequestedDisconnect
        finally:
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
            self._fragmenter.reset()
            self._disconnect_event = None
            self._put_state("DISCONNECTED", "stopped" if self._stop_requested.is_set() else "link lost")

    async def _write_gatt(self, payload: bytes) -> int:
        client = self._client
        if client is None or not getattr(client, "is_connected", False):
            raise TransportNotSent("BLE is not connected; command was not sent")
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
                connectionGeneration=self.connectionGeneration,
            )
            self._enqueue(envelope, _payload_priority(out_channel, payload))
        self._maybe_emit_diagnostics(now_ns)

    def _notify_for_attempt(self, attempt_generation: int, channel: str, data: bytes) -> None:
        """Reject callbacks retained by Bleak after a reconnect/stop."""

        normalized_channel = normalize_ble_channel(channel)
        if attempt_generation != self.connectionAttemptGeneration or self._stop_requested.is_set():
            key = normalized_channel if normalized_channel in self._notify_failures else "log"
            self._notify_failures[key] += 1
            return
        self._notify(normalized_channel, data)

    def _put_state(self, state: str, message: str) -> None:
        event = TransportStateEvent(
            "ble",
            state,
            self.sessionGeneration,
            message,
            {
                "connectionGeneration": self.connectionGeneration,
                "connectionAttemptGeneration": self.connectionAttemptGeneration,
                "reconnectAttempt": self.reconnectAttempt,
                "reconnectBackoff": self.reconnectBackoff,
                "address": self.address,
                "queueCounters": dict(self.queueCounters),
                "priorityBacklog": self.priorityBacklog,
                "notificationCounts": dict(self._notify_counts),
                "notificationBytes": dict(self._notify_bytes),
            },
        )
        self._enqueue(event, "lifecycle")

    @property
    def priorityBacklog(self) -> int:
        with self._priority_lock:
            return len(self._priority_backlog)

    def _enqueue(self, item: TransportEnvelope | TransportStateEvent, priority: str) -> None:
        try:
            self.outputQueue.put_nowait(item)
            return
        except queue.Full:
            pass
        if priority in {"control", "lifecycle", "fault"}:
            with self._priority_lock:
                if len(self._priority_backlog) < self._priority_backlog.maxlen:
                    self._priority_backlog.append(item)
                    return
            self.queueCounters[f"{priority}Drops"] += 1
            return
        if priority == "measurement":
            self.queueCounters["measurementDrops"] += 1
        else:
            self.queueCounters["diagnosticDrops"] += 1

    def _drain_priority_backlog(self) -> None:
        while True:
            with self._priority_lock:
                if not self._priority_backlog:
                    return
                item = self._priority_backlog[0]
            try:
                self.outputQueue.put_nowait(item)
            except queue.Full:
                return
            with self._priority_lock:
                if self._priority_backlog and self._priority_backlog[0] is item:
                    self._priority_backlog.popleft()

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
        missing = [channel for channel in ("ctrl", "data", "log") if channel not in mapping]
        if missing:
            raise RuntimeError(f"missing required SensorArray BLE characteristic(s): {', '.join(missing)}")
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
            connectionGeneration=self.connectionGeneration,
        )
        self._enqueue(envelope, "diagnostic")


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


def _payload_priority(channel: str, payload: bytes) -> str:
    if channel == "ctrl":
        return "control"
    if channel == "data":
        return "measurement"
    tag = payload.lstrip().split(b",", maxsplit=1)[0].decode("ascii", errors="ignore").upper()
    if tag in {"BOOT", "READY", "RST", "MAPP", "MERR", "RMAPP", "RMERR", "RAPP", "FAPP", "FERR"}:
        return "lifecycle"
    if tag in {"MFAULT", "APP_FATAL", "TXDROP", "CTRLDROP", "BLECORRUPT"} or "FAULT" in tag:
        return "fault"
    return "diagnostic"
