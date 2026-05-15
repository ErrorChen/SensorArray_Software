# Matrix Log Viewer

Dash-based SensorArray 8x8 matrix viewer for the Python host software.

## Firmware Baseline

This version targets:

```text
ErrorChen/SensorArray @ 4afe843e0648bfa6e482572593236b0bb84f09b9
short hash: 4afe843
title: UpperSpeed
```

`FAST_BINARY` is the default path. Legacy `MATV` CSV remains available for SAFE/CSV/debug firmware, but UpperSpeed FAST/BINARY should switch to pure binary after startup.

## Install And Run

```powershell
pip install -r requirements-dev.txt
python -m pytest
python main.py
```

Default URL:

```text
http://127.0.0.1:8050
```

Replay UpperSpeed samples:

```powershell
python matrix_log_viewer/generate_sample_fast_binary.py
python matrix_log_viewer/run_viewer.py --replay-file matrix_log_viewer/sample_logs/sample_upper_speed_startup_then_binary.bin --replay-speed 10
```

## UpperSpeed FAST/BINARY Startup

Startup may contain short ASCII diagnostics, including `RESET_REASON`, `APPMODE`, `BUILD_CONFIG`, `VOLTSCAN_CONFIG`, `STREAM_MEM`, `FAST_BINARY_DIAG`, `DBGROUTEPOLICY`, `DBGADSREFPOLICY`, `DBGTMUXPOLICY`, `ROUTE_POLICY`, and `ADS_POLICY`.

`FAST_BINARY_DIAG` reports bottleneck and writer counters such as `scanFps`, `outFps`, `qUsed`, `qFull`, `drop`, `shortWrite`, `writeFail`, `outputDiv`, `droppedBeforeFirstByte`, `partialAfterFirstByte`, `fullFrameWriteCount`, and `fullFrameWriteFailCount`.

The transition marker is:

```text
FAST_BINARY_START,magic=0x31434153,magicBytes=SAC1,version=1,frameType=0x1261,frameSize=312,pure=1
```

After this line stdout is pure binary. Do not use a text monitor or `readline()` to interpret the main stream after `FAST_BINARY_START`; the host must scan bytes for `SAC1` and validate CRC32.

## Pure Binary Frame

UpperSpeed compact frame:

```text
FMT = <IHHIQIIIIHHQ64iBBHI
SIZE = 312
magic bytes = SAC1
magic uint32 = 0x31434153
version = 1
frameType = 0x1261
frameTypeName = FAST_BINARY
CRC32 = binascii.crc32(rawFrame[:308]) & 0xffffffff
```

Values are `int32 microvolts[64]` ordered `S1D1..S1D8,S2D1..S8D8`. `validMask` bit index is `sourceZeroBased * 8 + detectorZeroBased`; when a bit is `0`, the cell is stored/rendered as `NaN` and old values are not reused.

The compact frame carries saturated 16-bit `droppedFrames` and `outputDecimatedFrames`. Full cumulative writer/drop counters are taken from `FAST_BINARY_DIAG`/startup status.

## Parser Behavior

The stream parser accepts `feed(data: bytes)` chunks from `serial.read(8192)`:

- Startup state parses complete ASCII diagnostics with replacement decoding while also looking for `SAC1`.
- `FAST_BINARY_START` validates `magicBytes=SAC1`, `version=1`, `frameType=0x1261`, and `frameSize=312`, then enters pure binary mode.
- Pure binary mode only scans for `SAC1`, requires 312 bytes, checks CRC32 over the first 308 bytes, decodes little-endian fields, and resyncs after CRC/magic errors.
- If ASCII such as `STAT`, `RATE_EVENT`, `MATV`, or CSV headers appears after `FAST_BINARY_START`, it is counted as `ASCII_AFTER_FAST_BINARY_START` protocol pollution, not as normal data.
- Non-UTF-8 bytes cannot crash the parser.

## Metrics

The UI deliberately separates these sources:

- `scanFps`: device scan rate.
- `outFps`: device output rate.
- `parsedFps`: host parser frame rate.
- `storedFps`: host store/CSV frame rate.
- `browserRafFps`: browser `requestAnimationFrame` refresh rate.
- `visualFps`: successful Plotly visual updates (`react`/`extendTraces`/`relayout`).
- `callbackFps`: Dash clientside live-update callback frequency.
- `DEVICE_DROP`: device-side passive drop.
- `DEVICE_DECIMATED`: active firmware output decimation.
- `OUTPUT_DIV`: active output divisor.
- `DROPPED_BEFORE_FIRST_BYTE`: whole compact frame dropped before writing any byte.
- `PARTIAL_AFTER_FIRST_BYTE`: serious protocol risk; must stay zero.
- `HOST_CRC`: host CRC failures.
- `HOST_RESYNC`: host magic resyncs/skipped binary bytes.
- `HOST_QUEUE_DROP`: Python input queue drops.
- `RENDER_SKIPPED`: GUI skipped intermediate display frames; this is not data loss.

If `partialAfterFirstByte > 0`, the GUI/CLI reports:

```text
PROTOCOL_RISK: firmware reported partialAfterFirstByte > 0
```

## GUI Performance

Receive/parse/store runs independently from browser rendering. `MatrixDataStore` uses numpy ring buffers and live history callbacks do not build pandas DataFrames. Python publishes lightweight snapshots; `assets/plotly_live_update.js` updates Plotly with `react`, `restyle`, `extendTraces`, `relayout`, and requestAnimationFrame coalescing. Snapshot revisions are acknowledged only after the relevant Plotly operation succeeds, so startup/reset snapshots are retried if the Dash graph div is not ready yet.

Recommended high-rate settings:

- Stream: `FAST_BINARY`
- Render mode: `Performance`
- GUI target fps: `60` or `30`
- Markers: off
- Advanced diagnostics: collapsed
- Heatmap text: off or 10 Hz
- History max points: 1000-1200

`GUI render interval ms` affects display refresh only. It does not change device sampling, parser throughput, or dataStore/CSV retention.

Manual history-graph acceptance after render-cache changes:

1. Start the GUI with no hardware connected. The status chips should show `raf fps` near the browser refresh rate, while `visual fps` can be 0 because no Plotly data is changing.
2. Connect a FAST_BINARY source and do not double-click the Plotly reset control. The history graph should reset on the first valid data snapshot, follow the latest selected window, and autoscale Y over the visible X range.
3. Change the selected cell, unit, stream, or history window. The history graph should reset once and then continue on the append path.
4. Click Clear. The backend store/cache and frontend Plotly state should both clear; the next valid snapshot should behave like first data without preserving the old axis range.
5. Manually zoom or pan the history X axis. Follow Latest should turn off. Clicking Follow Latest should immediately relayout the existing data back to the newest window, even before new serial data arrives.

## Binary Debug CLI

Use this before blaming the GUI:

```powershell
python matrix_log_viewer/read_matrix_binary.py --port COM5 --baud 921600 --print-matrix
```

Replay a capture/sample:

```powershell
python matrix_log_viewer/read_matrix_binary.py --input-file matrix_log_viewer/sample_logs/sample_upper_speed_crc_resync.bin
```

The CLI uses the same `SensorArrayStreamParser`, byte reads, optional raw dump, and optional wide CSV export. If the CLI is stable but the GUI is slow, the problem is rendering. If the CLI shows increasing `HOST_CRC`/`HOST_RESYNC`, investigate firmware config, flashing, USB path, captured bytes, or parser/frame format.

## Troubleshooting

- CLI fast, GUI slow: browser/Plotly/history rendering bottleneck.
- CLI CRC/resync spikes: protocol, firmware image, host read, or parser mismatch.
- `FAST_BINARY_START` followed by `STAT`/`MATV`: firmware config abnormal or stdout protocol pollution.
- `partialAfterFirstByte > 0`: serious protocol risk; a compact frame was partially written.
- `qFull`/`drop` increasing: firmware output queue cannot keep up.
- `decimated`/`outputDiv` increasing: auto rate control is protecting output.
- `RENDER_SKIPPED` increasing alone: GUI is dropping display refreshes, not stored data.
