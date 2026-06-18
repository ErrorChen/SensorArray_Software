from __future__ import annotations

from dataclasses import dataclass, field

from sensorarray_app.protocol.crc import crc32_reflected


@dataclass
class FragmentStats:
    received: int = 0
    duplicate: int = 0
    missing: int = 0
    timeout: int = 0
    reassembled: int = 0
    crcFailure: int = 0
    lengthFailure: int = 0


@dataclass
class _Message:
    channel: str
    messageId: int
    fragmentCount: int
    messageLen: int
    messageCrc32: int
    firstSeenNs: int
    fragments: dict[int, bytes] = field(default_factory=dict)


class BleFragmentReassembler:
    """Reassemble current and legacy G fragments by channel/message id."""

    def __init__(self, timeout_ns: int = 2_000_000_000):
        self.timeout_ns = int(timeout_ns)
        self._messages: dict[tuple[str, int], _Message] = {}
        self.stats = FragmentStats()

    def feed(self, channel: str, payload: bytes, now_ns: int) -> list[tuple[str, bytes]]:
        self._expire(now_ns)
        if not payload.startswith(b"G,"):
            return [(channel, payload)]
        header, sep, body = payload.partition(b"\n")
        if not sep:
            self.stats.lengthFailure += 1
            return []
        try:
            parts = header.decode("ascii", errors="strict").split(",")
            frag_channel = parts[1] or channel
            mid = int(parts[2], 0)
            index = int(parts[3], 0)
            count = int(parts[4], 0)
            payload_len = int(parts[5], 0)
            message_len = int(parts[6], 0)
            message_crc = int(parts[7], 16)
        except (UnicodeDecodeError, IndexError, ValueError):
            self.stats.lengthFailure += 1
            return []
        if payload_len != len(body):
            self.stats.lengthFailure += 1
            return []
        key = (frag_channel, mid)
        message = self._messages.get(key)
        if message is None:
            message = _Message(frag_channel, mid, count, message_len, message_crc, now_ns)
            self._messages[key] = message
        if index in message.fragments:
            self.stats.duplicate += 1
            return []
        self.stats.received += 1
        message.fragments[index] = body
        if len(message.fragments) != message.fragmentCount:
            return []
        missing = [idx for idx in range(message.fragmentCount) if idx not in message.fragments]
        if missing:
            self.stats.missing += 1
            return []
        assembled = b"".join(message.fragments[idx] for idx in range(message.fragmentCount))
        del self._messages[key]
        if len(assembled) != message.messageLen:
            self.stats.lengthFailure += 1
            return []
        if crc32_reflected(assembled) != message.messageCrc32:
            self.stats.crcFailure += 1
            return []
        self.stats.reassembled += 1
        return [(message.channel, assembled)]

    def _expire(self, now_ns: int) -> None:
        expired = [key for key, msg in self._messages.items() if now_ns - msg.firstSeenNs > self.timeout_ns]
        for key in expired:
            self.stats.timeout += 1
            del self._messages[key]
