from __future__ import annotations

import argparse
import time
from pathlib import Path

import numpy as np

from matrix_log_viewer.app import _build_compact_status_bar, _convert_matrix_for_display, _warning_badge
from matrix_log_viewer.data_store import MatrixDataStore
from matrix_log_viewer.protocol_parser import SensorArrayStreamParser
from matrix_log_viewer.render_cache import HeatmapRenderCacheThread, HistoryRenderCacheThread


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay a SensorArray capture through parser/store/render helpers.")
    parser.add_argument("--replay-file", required=True, help="Binary or mixed text/binary capture to replay")
    parser.add_argument("--chunk-size", type=int, default=4096)
    args = parser.parse_args()

    replay_path = Path(args.replay_file)
    if not replay_path.exists():
        print(f"SKIP: replay file does not exist: {replay_path}")
        return 0

    stream_parser = SensorArrayStreamParser()
    data_store = MatrixDataStore(maxPointsPerCell=20000)
    frame_count = 0
    byte_count = 0
    started = time.monotonic()
    with replay_path.open("rb") as replay_file:
        while True:
            chunk = replay_file.read(max(1, int(args.chunk_size)))
            if not chunk:
                break
            byte_count += len(chunk)
            for result in stream_parser.feedBytes(chunk):
                if result.frame is not None:
                    data_store.addFrame(result.frame)
                    frame_count += 1
                if result.status is not None:
                    data_store.addDeviceStatus(result.status)
                if result.event is not None:
                    data_store.addDeviceEvent(result.event)

    elapsed = max(1e-6, time.monotonic() - started)
    if frame_count == 0:
        print(f"ERROR: no matrix frames parsed from {replay_path}")
        return 1

    matrix, meta, _revision = data_store.getLatestMatrixAndMeta("FAST_BINARY")
    display_matrix, display_unit = _convert_matrix_for_display(matrix, meta.get("unit") or "uV", "auto")
    parser_stats = stream_parser.getStats()
    runtime_stats = {
        "parsedBinaryFps": frame_count / elapsed,
        "renderTickFps": 0.0,
        "renderedFrameFps": 0.0,
        "deviceSummary": data_store.getLatestDeviceStatus().get("summary", {}),
    }
    warning_badge, _warning_state = _warning_badge(parser_stats, {}, runtime_stats, meta, {})
    _build_compact_status_bar(
        meta=meta,
        matrix=display_matrix if isinstance(display_matrix, np.ndarray) else np.full((8, 8), np.nan),
        selected_cell="S1D1",
        parser_stats=parser_stats,
        connection_status={},
        runtime_stats=runtime_stats,
        paused=False,
        display_unit=display_unit,
        display_type=data_store.resolveFrameType("FAST_BINARY"),
        warning_badge=warning_badge,
    )

    heatmap_cache = HeatmapRenderCacheThread(data_store, targetFps=30)
    heatmap_cache.updateControls(stream="FAST_BINARY", selectedCell="S1D1", unitMode="auto", colorMode="auto")
    history_cache = HistoryRenderCacheThread(data_store, targetFps=30)
    history_cache.updateControls(stream="FAST_BINARY", selectedCell="S1D1", unitMode="auto", historyWindow="last_30s")
    heatmap_snapshot = heatmap_cache.getLatest() or {}
    history_snapshot = history_cache.getLatest() or {}

    print(f"replay_file: {replay_path}")
    print(f"bytes: {byte_count}")
    print(f"frames: {frame_count}")
    print(f"parser/store fps: {frame_count / elapsed:.1f}")
    print(f"heatmap snapshot: {heatmap_snapshot.get('stream', '-')} unit={heatmap_snapshot.get('unit', '-')}")
    print(f"history points: {len(history_snapshot.get('x') or [])}")
    print("gui helpers: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
