from __future__ import annotations

import queue
import socket
import threading
import time

from sensorarray_app.constants import WIFI_CTRL_PORT, WIFI_DATA_PORT, WIFI_LOG_PORT
from sensorarray_app.domain.models import TransportEnvelope, TransportStateEvent


class WifiUdpTransport:
    source = "wifi"

    def __init__(self, output_queue: "queue.Queue", session_generation: int, host: str):
        self.outputQueue = output_queue
        self.sessionGeneration = int(session_generation)
        self.host = host
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._ctrl_socket: socket.socket | None = None

    def start(self) -> None:
        for channel, port in (("data", WIFI_DATA_PORT), ("log", WIFI_LOG_PORT), ("ctrl", WIFI_CTRL_PORT)):
            thread = threading.Thread(target=self._listen, args=(channel, port), name=f"SensorArrayWifi{channel}", daemon=True)
            self._threads.append(thread)
            thread.start()
        self._ctrl_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._put_state("STREAMING", self.host)

    def stop(self) -> None:
        self._stop.set()
        if self._ctrl_socket is not None:
            self._ctrl_socket.close()
            self._ctrl_socket = None
        for thread in self._threads:
            thread.join(timeout=1.5)

    def send_command(self, command: str) -> None:
        self.write((command.rstrip() + "\n").encode("ascii", errors="strict"))

    def write(self, data: bytes) -> int:
        sock = self._ctrl_socket or socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        payload = bytes(data)
        return int(sock.sendto(payload, (self.host, WIFI_CTRL_PORT)))

    def _listen(self, channel: str, port: int) -> None:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("", port))
                sock.settimeout(0.2)
                while not self._stop.is_set():
                    try:
                        data, address = sock.recvfrom(8192)
                    except socket.timeout:
                        continue
                    envelope = TransportEnvelope(
                        source="wifi",
                        channel=channel,  # type: ignore[arg-type]
                        deviceId=self.host,
                        sessionGeneration=self.sessionGeneration,
                        receivedMonotonicNs=time.monotonic_ns(),
                        receivedWallTime=time.time(),
                        rawPayload=data,
                        remoteAddress=f"{address[0]}:{address[1]}",
                    )
                    try:
                        self.outputQueue.put_nowait(envelope)
                    except queue.Full:
                        pass
        except Exception as exc:
            self._put_state("ERROR", f"{channel}:{exc}")

    def _put_state(self, state: str, message: str) -> None:
        try:
            self.outputQueue.put_nowait(TransportStateEvent("wifi", state, self.sessionGeneration, message, {"host": self.host}))
        except queue.Full:
            pass
