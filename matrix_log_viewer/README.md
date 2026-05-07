# Matrix Log Viewer

Dash-based SensorArray 8x8 matrix viewer for the Python host software.

The host supports both input formats:

- FastSpeed binary `SAC1`, shown as stream `FAST_BINARY` and preferred by default.
- Legacy `MATV` CSV, kept for older firmware and debug configurations.

If `FAST_BINARY` frames have not arrived but `MATV` data has, the default view automatically falls back to `MATV`. The two streams remain stored independently and can be selected from the stream dropdown.

## Install

```powershell
cd matrix_log_viewer
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

For tests from the repository root:

```powershell
pip install -r requirements-dev.txt
python -m pytest
```

## Run

Recommended GUI entry point:

```powershell
python main.py
```

Initial serial connection from CLI:

```powershell
python matrix_log_viewer/run_viewer.py --port COM5 --baud 921600 --auto-reconnect
```

Replay legacy MATV:

```powershell
python matrix_log_viewer/run_viewer.py --replay-file matrix_log_viewer/sample_logs/sample_matv.log --replay-speed 10
```

Replay FastSpeed mixed binary/text:

```powershell
python matrix_log_viewer/run_viewer.py --replay-file matrix_log_viewer/sample_logs/sample_fast_binary_mixed.bin --replay-speed 10
```

Default URL:

```text
http://127.0.0.1:8050
```

`--baud` defaults to `921600`. For ESP32-S3 USB Serial/JTAG or USB CDC this value is mainly a host API parameter; do not treat it as the physical UART throughput limit.

## Binary Debug CLI

Use this first when separating firmware/serial/parser problems from GUI rendering problems:

```powershell
python matrix_log_viewer/read_matrix_binary.py --port COM5 --baud 921600 --print-matrix
```

Useful options:

```powershell
python matrix_log_viewer/read_matrix_binary.py --port COM5 --baud 921600 --duration 10 --save-csv output.csv --raw-dump output.bin
```

The tool uses `serial.read()` on bytes, feeds the same `SensorArrayStreamParser` as the GUI, and reports binary fps, text fps, latest seq, seq gap, scan duration, device dropped counters, CRC errors, resync count, and buffered bytes.

## FastSpeed Binary Protocol

Current Python format:

```text
FMT = <IHHIQIIIIIQ64iBBHI
SIZE = 312
magic bytes = SAC1
version = 1
frameType = 0x1261
frameTypeName = FAST_BINARY
```

CRC is IEEE CRC32 over `rawFrame[:SIZE-4]`. Values are signed int32 microvolts in `S1D1..S1D8,S2D1..S8D8` order. `validMask` bit `0` points are stored and rendered as `NaN`. The raw 312-byte frame is retained on each parsed `MatrixFrame` for debug/export paths.

Text rows such as `STAT`, `EVENT`, `RATE_EVENT`, `RATE_FATAL`, `DBG`, `APPMODE`, `VOLTSCAN_INIT`, `VOLTSCAN_GAIN`, and `VOLTSCAN_FATAL` update status/event panels only. They do not create matrix frames unless they are legacy `MATV` rows.

## Serial And Replay Read Strategy

- Serial and replay readers read bytes chunks, not lines.
- Default `readSize` is `8192` bytes and is configurable by CLI/GUI.
- Serial timeout is short, about `0.05 s`, so disconnect/reconnect remains responsive.
- Input queues are finite. If full, the reader drops the oldest chunk and records `droppedInputChunks` / `droppedInputBytes` instead of blocking the read thread.
- Replay files are opened in `rb` mode and parsed by the same mixed stream parser as live serial input.

## Mixed Stream Resync Strategy

The stream parser searches for `b"SAC1"` before treating bytes as text:

- If the buffer starts with `SAC1` and has at least `312` bytes, parse a binary frame first.
- If the buffer starts with `SAC1` but is short, wait for more bytes.
- If a complete text line appears before the next magic, parse it as text with replacement decoding.
- If garbage appears before magic, skip to magic and count resync/skipped bytes.
- On CRC failure, discard that candidate frame, count `binaryCrcErrors`, and resume scanning.
- Non-UTF-8 text bytes cannot crash the parser.

Parser stats include parsed binary/text frames, statuses/events, CRC errors, short frames, magic resyncs, skipped bytes/lines, parse errors, buffered bytes, last error/warning, and decoded status code name.

## GUI Behavior

- Stream dropdown defaults to `FAST_BINARY`.
- If no `FAST_BINARY` data exists but `MATV` exists, the matrix and history fall back to `MATV` and show a fallback note.
- Heatmap shows the latest 8x8 values and invalid cells as `NaN`/invalid.
- Clicking a cell updates the selected `SxDy`; the right history graph continues drawing that cell.
- Normal View is the default operator view: compact connection controls, stream/cell/window/unit/color controls, 8x8 heatmap, and selected-cell history.
- Replay controls, baud/read size, auto reconnect, custom x ranges, fixed color ranges, parser counters, connection internals, and device status are under **Advanced / Diagnostics**.
- History uses Plotly `Scattergl` in line mode by default. Markers are available in Advanced because they are expensive at high point counts.
- Auto follow on: x-axis follows the latest selected window.
- Auto follow off: `uirevision` keeps manual zoom/pan stable across refreshes.
- Receive/parse/store run in a background ingest thread. Dash uses separate intervals for lightweight ingest revision publication, graph rendering, compact status, and diagnostics.
- Graph rendering defaults to `50 ms` and is revision-gated: if no new frame or relevant control change arrived, graph callbacks return `no_update`.
- Compact status updates at `500 ms`; Advanced diagnostics update at `1000 ms` and do not redraw while the details panel is closed.
- Display downsampling applies only to the visible history window, not to raw in-memory storage or CSV export.

## GUI Performance Acceptance

Using `sample_fast_binary_mixed.bin` or a simulated 20 fps FastSpeed binary stream, Normal View should keep rendered GUI fps close to input fps, with a target of at least 15 fps on a typical lab laptop. The 30 s history window should scroll smoothly, switching cells should redraw history within one render tick, and expanding Advanced may reduce visual fps without slowing parsing/storage. The default first screen is intentionally dominated by the 8x8 heatmap and selected-cell history rather than parser/device debug fields.

## Status Panels

The UI separates:

- Device side: `droppedFrames`, `outputDecimatedFrames` / `STAT decimated`, `statusFlags`, decoded status codes.
- Host side: input queue drops, CRC errors, parser resyncs, parse errors.
- Throughput: bytes/sec, parsed binary fps, parsed text fps, GUI displayed fps, latest seq, seq gap.
- Display: rendered points and whether GUI history downsampling was applied.

## Troubleshooting

- Only `MATV` appears: firmware is still outputting legacy CSV.
- `STREAM_INIT` appears but no `FAST_BINARY`: check that binary frames are emitted and CRC/size match `312`.
- `binaryCrcErrors` increases: check struct format, frame size, mixed stream boundaries, and CRC calculation.
- `read_matrix_binary.py` reaches 25-31 fps but GUI looks slower: Plotly/Dash rendering or history window is the bottleneck.
- `read_matrix_binary.py` is also slow: firmware output rate, USB serial path, or parser resync/CRC errors are upstream bottlenecks.
- Device `droppedFrames` increases: device-side output queue/stdout cannot keep up.
- Host `droppedInputChunks` increases: Python host reading/parsing/storage cannot keep up with the incoming stream.
