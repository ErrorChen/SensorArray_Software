# Matrix Log Viewer

Matrix Log Viewer is the Dash web viewer for the SensorArray 8x8 matrix stream.

Protocol baseline:

- Hardware: `ErrorChen/SensorArray main@6886bfb` (`FastSpeed`)
- Software: `ErrorChen/SensorArray_Software main@0e80d7c` (`graph`)
- Do not use `c935f18 RollBack2` as the host protocol baseline. FastSpeed changed the default output.

FastSpeed default output is a compact binary frame (`SAC1`) plus periodic `STAT` text. Legacy `MATV` CSV may be disabled by firmware config and is no longer assumed to be the default.

## Install

```powershell
cd matrix_log_viewer
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

For tests:

```powershell
pip install -r ..\requirements-dev.txt
```

## Run

Recommended:

```powershell
python main.py
```

Then open the web UI and choose a COM port in the Connection panel.

CLI initial serial connection:

```powershell
python matrix_log_viewer/run_viewer.py --port COM5 --baud 115200 --auto-reconnect
```

Replay:

```powershell
python matrix_log_viewer/run_viewer.py --replay-file sample_logs/sample_matv.log --replay-speed 10
```

Start disconnected:

```powershell
python matrix_log_viewer/run_viewer.py
```

Default URL:

```text
http://127.0.0.1:8050
```

Use `--no-browser` if you do not want the launcher to open a browser automatically.

## Supported Input

FastSpeed binary:

- `sensorarrayVoltageCompactFrame_t`
- magic `0x31434153`, bytes `SAC1`
- little-endian struct format `<IHHIQIIIIIIQ64iBBHI`
- version `1`
- frame type `0x1261`
- IEEE CRC32 over all bytes before the `crc32` field
- value order `S1D1..S1D8,S2D1..S8D8`
- `microvolts[64]` are signed int32 `uV`

FastSpeed text:

- `STAT,...`
- `EVENT,...`
- `RATE_EVENT,...`
- `RATE_FATAL,...`
- `APPMODE,...`
- `VOLTSCAN_INIT,...`
- `VOLTSCAN_GAIN,...`
- `VOLTSCAN_FATAL,...`
- `DBG...`, `WARN...`, `ERROR...`

Legacy CSV:

- `MATV_HEADER,seq,timestamp_us,duration_us,unit,S1D1,...,S8D8`
- `MATV,<seq>,<timestamp_us>,<duration_us>,uV,<64 values>`
- `MATV_RAW`
- `MATV_GAIN`
- `MATV_ERR`
- Optional `MATV_RAW_HEADER`, `MATV_GAIN_HEADER`, `MATV_ERR_HEADER`

CSV parsing uses Python `csv.reader`. Unknown, empty, malformed, or non-UTF-8 text rows are counted and skipped without crashing the UI. `STAT`, `EVENT`, `DBG`, `APPMODE`, and `VOLTSCAN` rows update status/event panels and do not pollute matrix data.

## Web Connection

The Connection panel supports:

- Input Mode: Serial, Replay File, Disconnected
- Refresh Ports
- COM Port dropdown
- Baudrate
- Connect
- Disconnect
- Reconnect
- Auto reconnect
- Replay file path and replay speed

Refresh Ports does not interrupt an active connection. Connecting to a new COM port stops the old reader first. Disconnect can be clicked repeatedly. If `pyserial` is missing, the port list is empty and the UI shows the dependency error.

Common Windows COM port conflicts: VSCode serial monitor, `idf.py monitor`, Arduino IDE, PuTTY, and other serial terminals.

## Data Streams

The stream dropdown includes:

- `FAST_BINARY`
- `MATV`
- `MATV_RAW`
- `MATV_GAIN`
- `MATV_ERR`

Actual options are merged with streams seen in the input. `FAST_BINARY` is preferred by default. If no binary frames exist but legacy `MATV` data exists, the default display falls back to `MATV`.

FastSpeed metadata shown in the UI and CSV includes:

- `valid_mask`
- `status_flags`
- `first_status_code`
- `last_status_code`
- `dropped_frames`
- `output_decimated_frames`
- `ads_dr`
- `output_divider`

Invalid `validMask` points render as `NaN`/invalid in the heatmap. `droppedFrames` is passive loss from queue/stdout pressure. `outputDecimatedFrames` is active rate-control output skipping.

## Dynamic History Graph

The history graph uses Plotly `Scattergl`.

Controls:

- Auto follow latest
- X axis: `timeSeconds`, `timestampUs`, `seq`
- Window: All, Last 10 s, Last 30 s, Last 60 s, Last 5 min, Last N points, Custom range
- Last N points
- Custom x min / max

When Auto follow latest is enabled, the x-axis follows the selected latest window. When disabled, Plotly `uirevision` is kept stable so manual zoom and pan are not reset by refreshes.

For high-rate input, the graph down-samples only the displayed window to about 5000 points using min/max buckets. Raw in-memory history and CSV export are not down-sampled.

## Status Codes

Known FastSpeed codes are decoded in the UI, including:

- `0x0000 OK`
- `0x1001 ADS_SPI_FAIL`
- `0x1002 ADS_DRDY_TIMEOUT`
- `0x1003 ADS_CRC_FAIL`
- `0x1004 ADS_REG_VERIFY_FAIL`
- `0x1005 ADS_REF_POLICY_MISMATCH`
- `0x1006 ADS_GAIN_CHANGE_FAIL`
- `0x1007 ADS_DMA_FALLBACK`
- `0x1008 ADS_INPMUX_WRITE_FAIL`
- `0x1009 ADS_DIRECT_READ_FAIL`
- `0x100A ADS_STATUS_BYTE_BAD`
- `0x2001 TMUX_ROUTE_FAIL`
- `0x2002 TMUX_SW_POLICY_MISMATCH`
- `0x2003 TMUX_SOURCE_FAIL`
- `0x3001 STREAM_QUEUE_FULL`
- `0x3002 STREAM_FRAME_DROPPED`
- `0x3003 USB_STDOUT_BLOCKED`
- `0x3004 USB_STDOUT_WRITE_FAIL`
- `0x3005 USB_STDOUT_SHORT_WRITE`
- `0x4001 SPI_BUS_ACQUIRE_FAIL`
- `0x4002 SPI_BUS_RELEASE_FAIL`
- `0x5001 MODE_POLICY_MISMATCH`
- `0x6001 RATE_OUTPUT_DECIMATED`
- `0x6002 RATE_SCAN_THROTTLED`
- `0x6003 RATE_ADS_DR_REDUCED`
- `0x6004 RATE_MUX_SETTLE_INCREASED`
- `0x6005 RATE_VERIFIED_MUX_FORCED`
- `0x6006 RATE_SAFE_PROFILE_ENTERED`
- `0x6007 RATE_FATAL_STOP`
- `0x7FFF INTERNAL_ASSERT_FAIL`

Unknown codes display as `UNKNOWN_0xXXXX`.

## Test

From the repository root:

```powershell
.venv\Scripts\python.exe -m pytest
```

or install `pytest` first:

```powershell
pip install -r requirements-dev.txt
pytest
```
