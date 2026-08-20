from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field


@dataclass
class _CandidateSegment:
    start: int
    end: int
    step: int = 1
    phase: int = 0

    def count(self, lower: int | None = None, upper: int | None = None) -> int:
        start = self.start if lower is None else max(self.start, int(lower))
        end = self.end if upper is None else min(self.end, int(upper))
        if end < start:
            return 0
        first = start + ((self.phase - start) % self.step)
        return 0 if first > end else ((end - first) // self.step) + 1

    def consume(self, lower: int, upper: int, limit: int) -> tuple[int, list[_CandidateSegment]]:
        amount = min(max(0, int(limit)), self.count(lower, upper))
        if amount == 0:
            return 0, [self]
        overlap_start = max(self.start, int(lower))
        first = overlap_start + ((self.phase - overlap_start) % self.step)
        last = first + ((amount - 1) * self.step)
        remaining: list[_CandidateSegment] = []
        if self.count(self.start, first - 1):
            remaining.append(_CandidateSegment(self.start, first - 1, self.step, self.phase))
        if self.count(last + 1, self.end):
            remaining.append(_CandidateSegment(last + 1, self.end, self.step, self.phase))
        return amount, remaining


@dataclass
class _PendingSequenceGap:
    key: tuple[str, str]
    reason: str
    segments: list[_CandidateSegment]

    @property
    def remaining(self) -> int:
        return sum(segment.count() for segment in self.segments)

    def consume(self, lower: int, upper: int, limit: int) -> int:
        available = max(0, int(limit))
        consumed = 0
        updated: list[_CandidateSegment] = []
        for segment in self.segments:
            if available <= 0:
                updated.append(segment)
                continue
            amount, remaining = segment.consume(lower, upper, available)
            consumed += amount
            available -= amount
            updated.extend(remaining)
        self.segments = updated
        return consumed


@dataclass
class _FirmwareOutputWindow:
    end: int
    invalidFrames: int = 0
    invalidFramesUsed: int = 0
    firmwareDrops: int = 0


@dataclass
class StatisticsStore:
    transportBytes: int = 0
    transportPackets: int = 0
    parserFrames: int = 0
    parserRejects: int = 0
    crcFailures: int = 0
    sequenceGaps: int = 0
    observedSequenceGapFrames: int = 0
    intentionalFirmwareDecimation: int = 0
    firmwareSuppressedNonFresh: int = 0
    firmwareTransportDrop: int = 0
    firmwareAttributedSequenceGap: int = 0
    wireInterleaveRecoveries: int = 0
    wireInterleaveDroppedFrames: int = 0
    hostTransportDrop: int = 0
    unknownSequenceGap: int = 0
    displayCoalescing: int = 0
    fragmentDrops: int = 0
    hostQueueDrops: int = 0
    historyOverwrites: int = 0
    renderSkipped: int = 0
    reconnectCount: int = 0
    _samples: deque[tuple[float, str, int]] = field(default_factory=deque)
    byReason: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    _lastSequences: dict[tuple[str, str], int] = field(default_factory=dict)
    _firmwareDropReports: dict[str, tuple[int, tuple[str, str] | None, int | None]] = field(default_factory=dict)
    _firmwareWindowDropTotal: int = 0
    _firmwareCounterDropTotal: int = 0
    _pendingSequenceGaps: deque[_PendingSequenceGap] = field(default_factory=deque)
    _firmwareOutputWindows: dict[tuple[str, str, int], _FirmwareOutputWindow] = field(default_factory=dict)
    _firmwarePerformanceReports: dict[tuple[str, str], tuple[int, int, int | None, int]] = field(default_factory=dict)
    _firmwareSuppressedByKey: dict[tuple[str, str], int] = field(default_factory=lambda: defaultdict(int))

    def record_transport(self, byte_count: int) -> None:
        self.transportBytes += int(byte_count)
        self.transportPackets += 1
        self._sample("transport", 1)

    def record_frame(
        self,
        *,
        seq: int | None = None,
        source: str = "",
        boot_id: int | None = None,
        connection_generation: int = 0,
        usb_mode: str | None = None,
        data_every: int | None = None,
    ) -> None:
        self.parserFrames += 1
        self._sample("frame", 1)
        if seq is None:
            return
        key = self._sequence_key(source, boot_id, connection_generation)
        current = int(seq)
        previous = self._lastSequences.get(key)
        self._lastSequences[key] = current
        if previous is None or current <= previous + 1:
            if previous is not None and current <= previous:
                self.byReason["non_monotonic_sequence"] += 1
            return
        missing = current - previous - 1
        self.observedSequenceGapFrames += missing
        debug_decimation = (
            key[0] == "serial"
            and str(usb_mode or "").upper() == "DEBUG"
            and int(data_every or 0) > 1
        )
        if debug_decimation:
            every = int(data_every or 0)
            phase = previous % every
            candidates = _CandidateSegment(previous + 1, current - 1, every, phase)
            unexplained = candidates.count()
            intentional = missing - unexplained
            self.intentionalFirmwareDecimation += intentional
            if unexplained:
                self.unknownSequenceGap += unexplained
                self.sequenceGaps += unexplained
                self.byReason["debug_unexplained_sequence_gap"] += unexplained
                gap = _PendingSequenceGap(key, "debug_unexplained_sequence_gap", [candidates])
                self._pendingSequenceGaps.append(gap)
                self._reconcile_gap_with_windows(gap)
                self._prune_reconciled_gaps()
            return
        self.unknownSequenceGap += missing
        self.sequenceGaps += missing
        self.byReason["unknown_sequence_gap"] += missing
        gap = _PendingSequenceGap(
            key,
            "unknown_sequence_gap",
            [_CandidateSegment(previous + 1, current - 1)],
        )
        self._pendingSequenceGaps.append(gap)
        self._reconcile_gap_with_windows(gap)
        self._prune_reconciled_gaps()

    def record_firmware_output_window(
        self,
        *,
        source: str,
        boot_id: int | None,
        connection_generation: int,
        sequence_start: int,
        sequence_end: int,
        frame_count: int,
        invalid_frames: int,
        firmware_drops: int = 0,
    ) -> None:
        """Reconcile sequence gaps with one firmware SF50 reporting window.

        SF50 is cumulative within its current 50-physical-frame window and may
        also be emitted as partial USB diagnostics.  Keying by transport,
        boot/connection identity and window start prevents a partial 15/30/45
        report from being counted more than once.
        """

        start = int(sequence_start)
        end = int(sequence_end)
        count = int(frame_count)
        invalid = max(0, int(invalid_frames))
        drops = max(0, int(firmware_drops))
        if start < 0 or end < start or count <= 0 or invalid > count:
            return
        key = self._sequence_key(source, boot_id, connection_generation)
        window_key = (key[0], key[1], start)
        window = self._firmwareOutputWindows.get(window_key)
        if window is None:
            window = _FirmwareOutputWindow(end=end)
            self._firmwareOutputWindows[window_key] = window
        window.end = max(window.end, end)
        window.invalidFrames = max(window.invalidFrames, invalid)
        if drops > window.firmwareDrops:
            self._firmwareWindowDropTotal += drops - window.firmwareDrops
            window.firmwareDrops = drops
            self._refresh_firmware_drop_total()
        self._reconcile_window(key, start, window)
        # SF50 drop=0/<text-bus>/<all-sinks> is useful health evidence but its
        # aggregate sink count cannot prove that this source lost a
        # measurement packet.  Sequence attribution is therefore deferred to
        # a source-specific counter (PERF usbDrop / BL50 dropD) after all
        # non-fresh evidence for the interval has been applied.
        self._prune_reconciled_gaps()
        while len(self._firmwareOutputWindows) > 256:
            self._firmwareOutputWindows.pop(next(iter(self._firmwareOutputWindows)))

    def record_firmware_performance_counters(
        self,
        *,
        source: str,
        boot_id: int | None,
        connection_generation: int,
        published_frames: int,
        fresh_frames: int,
        sequence_end: int | None = None,
    ) -> None:
        """Use consecutive PERF snapshots to close the trailing SF50 window."""

        published = max(0, int(published_frames))
        fresh = max(0, int(fresh_frames))
        if fresh > published:
            return
        key = self._sequence_key(source, boot_id, connection_generation)
        observed_sequence = self._lastSequences.get(key)
        report_sequence = observed_sequence
        if sequence_end is not None:
            firmware_sequence = max(0, int(sequence_end))
            report_sequence = (
                firmware_sequence
                if observed_sequence is None
                else min(observed_sequence, firmware_sequence)
            )
        previous = self._firmwarePerformanceReports.get(key)
        classified_total = self._firmwareSuppressedByKey[key]
        if previous is None or published < previous[0] or fresh < previous[1]:
            self._firmwarePerformanceReports[key] = (
                published,
                fresh,
                report_sequence,
                classified_total,
            )
            return
        invalid_delta = (published - previous[0]) - (fresh - previous[1])
        # SF50 may already have attributed part (or all) of this same PERF
        # interval. PERF closes only the still-unreported tail; it must never
        # spend the same firmware non-fresh frame twice.
        already_classified = max(0, classified_total - previous[3])
        invalid_delta = max(0, invalid_delta - already_classified)
        if invalid_delta > 0 and report_sequence is not None:
            previous_sequence = previous[2]
            lower = 0 if previous_sequence is None else previous_sequence + 1
            self._reclassify_pending(key, lower, report_sequence, invalid_delta)
        self._prune_reconciled_gaps()
        self._firmwarePerformanceReports[key] = (
            published,
            fresh,
            report_sequence,
            self._firmwareSuppressedByKey[key],
        )

    def record_firmware_drop_report(
        self,
        report_key: str,
        total: int,
        *,
        source: str = "",
        boot_id: int | None = None,
        connection_generation: int = 0,
        attribute_sequence: bool = False,
        baseline_first: bool = False,
        sequence_end: int | None = None,
    ) -> None:
        report_name = str(report_key or "firmware")
        value = max(0, int(total))
        sequence_key = self._sequence_key(source, boot_id, connection_generation) if source else None
        last_sequence = self._lastSequences.get(sequence_key) if sequence_key is not None else None
        if sequence_end is not None:
            firmware_sequence = max(0, int(sequence_end))
            last_sequence = firmware_sequence if last_sequence is None else min(last_sequence, firmware_sequence)
        previous = self._firmwareDropReports.get(report_name)
        previous_total = previous[0] if previous is not None else None
        delta = (
            0
            if previous_total is None and baseline_first
            else value
            if previous_total is None
            else max(0, value - previous_total)
        )
        self._firmwareCounterDropTotal += delta
        self._refresh_firmware_drop_total()
        if attribute_sequence and sequence_key is not None and last_sequence is not None and delta > 0:
            previous_sequence = previous[2] if previous is not None and previous[1] == sequence_key else None
            lower = 0 if previous_sequence is None else previous_sequence + 1
            self._attribute_pending_firmware_drop(sequence_key, lower, last_sequence, delta)
            self._prune_reconciled_gaps()
        self._firmwareDropReports[report_name] = (value, sequence_key, last_sequence)

    def begin_output_policy(
        self,
        *,
        source: str,
        boot_id: int | None,
        connection_generation: int,
    ) -> None:
        """Start a new received-sequence baseline after DEBUG/FULL changes."""

        key = self._sequence_key(source, boot_id, connection_generation)
        self._lastSequences.pop(key, None)
        self._firmwarePerformanceReports.pop(key, None)

    def begin_connection_epoch(
        self,
        *,
        source: str,
        boot_id: int | None,
        connection_generation: int,
        reconnect: bool,
    ) -> None:
        """Close receive-side sequence state at a physical link boundary.

        Firmware sequence numbers continue while a BLE/serial link is down.
        The first frame after reconnect therefore starts a new receive
        baseline; the skipped device frames are represented by the explicit
        transport discontinuity instead of being mislabelled as Host loss.
        """

        key = self._sequence_key(source, boot_id, connection_generation)
        self._lastSequences.pop(key, None)
        self._firmwarePerformanceReports.pop(key, None)
        # Evidence arriving in the new connection epoch must not reconcile a
        # still-open candidate range from the old link.
        self._pendingSequenceGaps = deque(
            gap for gap in self._pendingSequenceGaps if gap.key != key
        )
        for window_key in tuple(self._firmwareOutputWindows):
            if window_key[:2] == key:
                self._firmwareOutputWindows.pop(window_key, None)
        if reconnect:
            self.reconnectCount += 1

    def record_host_transport_drop(self, count: int = 1) -> None:
        amount = max(0, int(count))
        self.hostTransportDrop += amount
        self.hostQueueDrops += amount

    def record_reject(self, reason: str) -> None:
        self.parserRejects += 1
        self.byReason[reason] += 1

    def record_wire_interleave_recovery(self, *, dropped_pending_frame: bool) -> None:
        self.wireInterleaveRecoveries += 1
        if dropped_pending_frame:
            self.wireInterleaveDroppedFrames += 1

    def snapshot(self, visual_fps: float = 0.0, stored_fps: float = 0.0) -> dict:
        now = time.monotonic()
        while self._samples and self._samples[0][0] < now - 5.0:
            self._samples.popleft()
        duration = max(1e-6, now - self._samples[0][0]) if self._samples else 1.0
        parser_count = sum(amount for _, kind, amount in self._samples if kind == "frame")
        pending_firmware_evidence = self._pending_firmware_evidence_count()
        return {
            "transportBytes": self.transportBytes,
            "transportPackets": self.transportPackets,
            "parserFrames": self.parserFrames,
            "parserRejects": self.parserRejects,
            "crcFailures": self.crcFailures,
            "sequenceGaps": self.sequenceGaps,
            "observedSequenceGapFrames": self.observedSequenceGapFrames,
            "intentionalFirmwareDecimation": self.intentionalFirmwareDecimation,
            "firmwareSuppressedNonFresh": self.firmwareSuppressedNonFresh,
            "expectedOutputDecimation": self.intentionalFirmwareDecimation + self.firmwareSuppressedNonFresh,
            "firmwareTransportDrop": self.firmwareTransportDrop,
            "firmwareReportedDrop": self.firmwareTransportDrop,
            "firmwareAttributedSequenceGap": self.firmwareAttributedSequenceGap,
            "wireInterleaveRecoveries": self.wireInterleaveRecoveries,
            "wireInterleaveDroppedFrames": self.wireInterleaveDroppedFrames,
            "hostTransportDrop": self.hostTransportDrop,
            "unknownSequenceGap": self.unknownSequenceGap,
            "pendingFirmwareEvidenceGap": pending_firmware_evidence,
            "hostUnexplainedSequenceGap": max(0, self.unknownSequenceGap - pending_firmware_evidence),
            "displayCoalescing": self.displayCoalescing,
            "fragmentDrops": self.fragmentDrops,
            "hostQueueDrops": self.hostQueueDrops,
            "historyOverwrites": self.historyOverwrites,
            "renderSkipped": self.renderSkipped,
            "visualFps": visual_fps,
            "parserFps": parser_count / duration,
            "storedFps": stored_fps,
            "reconnectCount": self.reconnectCount,
            "rejectsByReason": dict(self.byReason),
        }

    def _sample(self, kind: str, amount: int) -> None:
        self._samples.append((time.monotonic(), kind, int(amount)))

    @staticmethod
    def _sequence_key(
        source: str,
        boot_id: int | None,
        connection_generation: int,
    ) -> tuple[str, str]:
        identity = (
            f"boot:{int(boot_id)}"
            if boot_id is not None
            else f"connection:{int(connection_generation)}"
        )
        return str(source or "unknown").lower(), identity

    def _reconcile_gap_with_windows(self, gap: _PendingSequenceGap) -> None:
        for (source, identity, start), window in self._firmwareOutputWindows.items():
            if (source, identity) == gap.key:
                self._reconcile_window(gap.key, start, window, only_gap=gap)

    def _reconcile_window(
        self,
        key: tuple[str, str],
        start: int,
        window: _FirmwareOutputWindow,
        *,
        only_gap: _PendingSequenceGap | None = None,
    ) -> None:
        available = max(0, window.invalidFrames - window.invalidFramesUsed)
        if available <= 0:
            return
        gaps = (only_gap,) if only_gap is not None else tuple(self._pendingSequenceGaps)
        for gap in gaps:
            if available <= 0:
                break
            if gap.key != key or gap.remaining <= 0:
                continue
            consumed = gap.consume(start, window.end, available)
            if consumed:
                self._reclassify(gap, consumed)
                window.invalidFramesUsed += consumed
                available -= consumed

    def _reclassify_pending(
        self,
        key: tuple[str, str],
        lower: int,
        upper: int,
        limit: int,
    ) -> None:
        available = max(0, int(limit))
        for gap in tuple(self._pendingSequenceGaps):
            if available <= 0:
                break
            if gap.key != key or gap.remaining <= 0:
                continue
            consumed = gap.consume(lower, upper, available)
            if consumed:
                self._reclassify(gap, consumed)
                available -= consumed

    def _reclassify(self, gap: _PendingSequenceGap, amount: int) -> None:
        count = max(0, int(amount))
        self.unknownSequenceGap = max(0, self.unknownSequenceGap - count)
        self.sequenceGaps = max(0, self.sequenceGaps - count)
        self.byReason[gap.reason] = max(0, self.byReason[gap.reason] - count)
        self.firmwareSuppressedNonFresh += count
        self._firmwareSuppressedByKey[gap.key] += count
        self.byReason["firmware_suppressed_non_fresh"] += count

    def _attribute_pending_firmware_drop(
        self,
        key: tuple[str, str],
        lower: int,
        upper: int,
        limit: int,
    ) -> None:
        available = min(
            max(0, int(limit)),
            max(0, self.firmwareTransportDrop - self.firmwareAttributedSequenceGap),
        )
        for gap in tuple(self._pendingSequenceGaps):
            if available <= 0:
                break
            if gap.key != key or gap.remaining <= 0:
                continue
            consumed = gap.consume(lower, upper, available)
            if consumed:
                self._attribute_firmware_drop(gap, consumed)
                available -= consumed

    def _attribute_firmware_drop(self, gap: _PendingSequenceGap, amount: int) -> None:
        count = max(0, int(amount))
        self.unknownSequenceGap = max(0, self.unknownSequenceGap - count)
        self.sequenceGaps = max(0, self.sequenceGaps - count)
        self.byReason[gap.reason] = max(0, self.byReason[gap.reason] - count)
        self.firmwareAttributedSequenceGap += count
        self.byReason["firmware_transport_drop_gap"] += count

    def _refresh_firmware_drop_total(self) -> None:
        # SF50/OT50 are window deltas while PERF exposes overlapping cumulative
        # counters.  Use the larger evidence total instead of double-counting
        # the same USB sink drop through both diagnostic families.
        self.firmwareTransportDrop = max(
            self._firmwareWindowDropTotal,
            self._firmwareCounterDropTotal,
        )

    def _pending_firmware_evidence_count(self) -> int:
        """Count gaps newer than the causal watermark of the latest PERF reply.

        A PERF line is queued behind the live measurement stream.  By the time
        Host receives it, later frames may already have exposed a gap that the
        firmware counter snapshot could not yet include.  Such a tail is
        neither explained nor proven Host loss until a later PERF watermark
        passes it.
        """

        pending = 0
        for gap in self._pendingSequenceGaps:
            report = self._firmwarePerformanceReports.get(gap.key)
            watermark = report[2] if report is not None else None
            if watermark is None:
                continue
            for segment in gap.segments:
                pending += segment.count(lower=watermark + 1)
        return min(self.unknownSequenceGap, pending)

    def _prune_reconciled_gaps(self) -> None:
        self._pendingSequenceGaps = deque(
            gap for gap in self._pendingSequenceGaps if gap.remaining > 0
        )
