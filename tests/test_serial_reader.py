from __future__ import annotations

import queue
import time
from types import SimpleNamespace

from matrix_log_viewer import serial_reader as serial_reader_module
from matrix_log_viewer.serial_reader import BaseReaderThread, SerialReaderThread


class FakeSerial:
    def __init__(self):
        self.read_calls = 0
        self.readline_calls = 0
        self.closed = False

    def read(self, size: int) -> bytes:
        self.read_calls += 1
        if self.read_calls == 1:
            return b"SAC1"
        time.sleep(0.01)
        return b""

    def readline(self) -> bytes:
        self.readline_calls += 1
        raise AssertionError("SerialReaderThread must not call readline()")

    def close(self) -> None:
        self.closed = True


def test_serial_reader_uses_read_not_readline(monkeypatch):
    fake = FakeSerial()
    monkeypatch.setattr(serial_reader_module, "serial", SimpleNamespace(Serial=lambda *args, **kwargs: fake))
    input_queue: queue.Queue[bytes] = queue.Queue(maxsize=4)

    reader = SerialReaderThread("COM5", 921600, input_queue, readSize=4096)
    reader.start()
    chunk = input_queue.get(timeout=1.0)
    reader.stop()
    reader.join(timeout=1.0)

    assert chunk == b"SAC1"
    assert fake.read_calls >= 1
    assert fake.readline_calls == 0
    assert fake.closed is True


def test_queue_full_drops_oldest_without_blocking():
    input_queue: queue.Queue[bytes] = queue.Queue(maxsize=1)
    input_queue.put_nowait(b"old")
    reader = BaseReaderThread("TestReader", "test", input_queue)

    reader._put_bytes(b"new")

    assert input_queue.get_nowait() == b"new"
    status = reader.getStatus()
    assert status["droppedInputChunks"] == 1
    assert status["droppedInputBytes"] == len(b"old")
