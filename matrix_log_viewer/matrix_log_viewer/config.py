from __future__ import annotations

MATRIX_SIZE = 8
DEFAULT_BAUD = 115200
DEFAULT_MAX_POINTS_PER_CELL = 5000
DEFAULT_DASH_HOST = "127.0.0.1"
DEFAULT_DASH_PORT = 8050
DEFAULT_REPLAY_SPEED = 1.0
DEFAULT_REFRESH_INTERVAL_MS = 500
MAX_LINES_PER_DASH_TICK = 1000

CELL_NAMES = tuple(
    f"S{source_index}D{detector_index}"
    for source_index in range(1, MATRIX_SIZE + 1)
    for detector_index in range(1, MATRIX_SIZE + 1)
)

SOURCE_LABELS = [f"S{index}" for index in range(1, MATRIX_SIZE + 1)]
DETECTOR_LABELS = [f"D{index}" for index in range(1, MATRIX_SIZE + 1)]

WIDE_CSV_COLUMNS = [
    "seq",
    "timestamp_us",
    "time_s",
    "duration_us",
    "unit",
    *CELL_NAMES,
]

