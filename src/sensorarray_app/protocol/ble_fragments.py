from __future__ import annotations

from dataclasses import dataclass, field

from sensorarray_app.protocol.crc import crc32_reflected

_CHANNEL_ALIASES = {
    "data": "data",
    "d": "data",
    "cap": "data",
    "caps": "data",
    "c": "data",
    "capacitance": "data",
    "log": "log",
    "logs": "log",
    "l": "log",
    "ctrl": "ctrl",
    "control": "ctrl",
    "cmd": "ctrl",
    "command": "ctrl",
}


def normalize_ble_channel(channel: str | None, fallback: str = "data") -> str:
    """Normalize firmware BLE channel shorthands without hiding unknown values."""

    text = str(channel or "").strip()
    if not text:
        return normalize_ble_channel(fallback, "data")
    return _CHANNEL_ALIASES.get(text.lower(), text)


@dataclass
class FragmentStats:
    received: int = 0
    duplicate: int = 0
    missing: int = 0
    timeout: int = 0
    reassembled: int = 0
    crcFailure: int = 0
    lengthFailure: int = 0
    unknownChannel: int = 0


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
        input_channel = normalize_ble_channel(channel)
        output: list[tuple[str, bytes]] = []
        offset = 0
        while offset < len(payload):
            if payload.startswith(b"G,", offset):
                next_offset, assembled = self._feed_fragment(input_channel, payload, offset, now_ns)
                offset = next_offset
                output.extend(assembled)
                continue
            next_fragment = payload.find(b"\nG,", offset)
            if next_fragment < 0:
                raw_payload = payload[offset:]
                offset = len(payload)
            else:
                raw_payload = payload[offset : next_fragment + 1]
                offset = next_fragment + 1
            if raw_payload:
                output.append((input_channel, raw_payload))
        return output

    def reset(self) -> None:
        """Discard partial messages at a transport-session boundary."""

        self._messages.clear()

    def _feed_fragment(
        self,
        input_channel: str,
        payload: bytes,
        offset: int,
        now_ns: int,
    ) -> tuple[int, list[tuple[str, bytes]]]:
        header_end = payload.find(b"\n", offset)
        if header_end < 0:
            self.stats.lengthFailure += 1
            return len(payload), []
        header = payload[offset:header_end]
        try:
            parts = header.decode("ascii", errors="strict").split(",")
            raw_channel = parts[1] if len(parts) > 1 else ""
            frag_channel = normalize_ble_channel(raw_channel, input_channel)
            if raw_channel and frag_channel == raw_channel and frag_channel not in {"data", "log", "ctrl"}:
                self.stats.unknownChannel += 1
            mid = int(parts[2], 0)
            index = int(parts[3], 0)
            count = int(parts[4], 0)
            payload_len = int(parts[5], 0)
            message_len = int(parts[6], 0)
            message_crc = int(parts[7], 16)
        except (UnicodeDecodeError, IndexError, ValueError):
            self.stats.lengthFailure += 1
            return header_end + 1, []
        body_start = header_end + 1
        body_end = body_start + payload_len
        if payload_len < 0 or body_end > len(payload):
            self.stats.lengthFailure += 1
            return len(payload), []
        body = payload[body_start:body_end]
        if count <= 0 or index < 0 or index >= count:
            self.stats.lengthFailure += 1
            return body_end, []
        key = (frag_channel, mid)
        message = self._messages.get(key)
        if message is None:
            message = _Message(frag_channel, mid, count, message_len, message_crc, now_ns)
            self._messages[key] = message
        if (
            message.fragmentCount != count
            or message.messageLen != message_len
            or message.messageCrc32 != message_crc
        ):
            self.stats.lengthFailure += 1
            del self._messages[key]
            return body_end, []
        if index in message.fragments:
            self.stats.duplicate += 1
            return body_end, []
        self.stats.received += 1
        message.fragments[index] = body
        if len(message.fragments) != message.fragmentCount:
            return body_end, []
        missing = [idx for idx in range(message.fragmentCount) if idx not in message.fragments]
        if missing:
            self.stats.missing += 1
            del self._messages[key]
            return body_end, []
        assembled = b"".join(message.fragments[idx] for idx in range(message.fragmentCount))
        del self._messages[key]
        if len(assembled) != message.messageLen:
            self.stats.lengthFailure += 1
            return body_end, []
        if crc32_reflected(assembled) != message.messageCrc32:
            self.stats.crcFailure += 1
            return body_end, []
        self.stats.reassembled += 1
        return body_end, [(message.channel, assembled)]

    def _expire(self, now_ns: int) -> None:
        expired = [key for key, msg in self._messages.items() if now_ns - msg.firstSeenNs > self.timeout_ns]
        for key in expired:
            self.stats.timeout += 1
            del self._messages[key]
