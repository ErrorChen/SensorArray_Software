from __future__ import annotations

MATRIX_SIZE = 8
DEFAULT_BAUD = 921600
DEFAULT_SERIAL_READ_SIZE = 8192
DEFAULT_SERIAL_TIMEOUT_SECONDS = 0.05
DEFAULT_INPUT_QUEUE_MAX_CHUNKS = 1024
DEFAULT_MAX_POINTS_PER_CELL = 5000
DEFAULT_DASH_HOST = "127.0.0.1"
DEFAULT_DASH_PORT = 8050
DEFAULT_REPLAY_SPEED = 1.0
DEFAULT_REFRESH_INTERVAL_MS = 100
MAX_INPUT_CHUNKS_PER_DASH_TICK = 200
MAX_PARSE_RESULTS_PER_TICK = 5000
DISPLAY_DOWNSAMPLE_TARGET_POINTS = 5000

# Compatibility name for older callers.
MAX_LINES_PER_DASH_TICK = MAX_INPUT_CHUNKS_PER_DASH_TICK

CELL_NAMES = tuple(
    f"S{source_index}D{detector_index}"
    for source_index in range(1, MATRIX_SIZE + 1)
    for detector_index in range(1, MATRIX_SIZE + 1)
)

SOURCE_LABELS = [f"S{index}" for index in range(1, MATRIX_SIZE + 1)]
DETECTOR_LABELS = [f"D{index}" for index in range(1, MATRIX_SIZE + 1)]

FRAME_META_CSV_COLUMNS = [
    "frame_type",
    "seq",
    "timestamp_us",
    "time_s",
    "duration_us",
    "unit",
    "valid_mask",
    "status_flags",
    "first_status_code",
    "first_status_code_name",
    "last_status_code",
    "last_status_code_name",
    "dropped_frames",
    "output_decimated_frames",
    "ads_dr",
    "output_divider",
]

WIDE_CSV_COLUMNS = [
    *FRAME_META_CSV_COLUMNS,
    *CELL_NAMES,
]
