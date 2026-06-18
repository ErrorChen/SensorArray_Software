# SensorArray b41 Matrix Host

Host-side Dash/Plotly application for the SensorArray real-time 8x8 detection matrix.

Firmware protocol baseline is locked to:

```text
b41c5256fbb5b23a0f0d98ed651db2f6ced3a0d6
```

The host repository is the only repository modified by this refactor. The embedded repository is read only and is used only to confirm protocol, device names, BLE UUIDs, Wi-Fi UDP ports, ROWS command semantics, battery logs, and data formats.

## Interface

The main screen is organized as:

```text
8x8 Heatmap | Setup
            | Four-point trend
Raw Logs
```

Screenshot artifacts from visual validation are stored under `artifacts/ui/`.

The left panel is a fixed 8x8 heatmap with columns `D1..D8` and rows `S1..S8`, with `S1` at the top. Inactive rows are shown as invalid/NaN, are excluded from the color scale, and cannot be selected. The right setup panel contains Connection, Rows, Measurement, Baseline, Battery, and Diagnostics controls. The lower-right chart shows the four channels selected from one row and one FDC. The bottom terminal is the raw log ring.

## Current b41 Protocol

The b41 primary capacitance stream is compact ASCII C/D/K, not legacy FAST_BINARY:

```text
C,seq=301,ts=123456789,rows=5,cells=40,gen=12,rid=9,rf=1F,pf=1F,sf=1F,bad=0/0/0,fmt=pf6,n=40
D0,<up to 16 fixed-point integers>
D1,<up to 16 fixed-point integers>
D2,<short final line is legal>
K,seq=301,gen=12,rid=9,crc=89ABCDEF
```

CRC is standard reflected CRC-32 over exact ASCII bytes from the `C` line through the last `D` line, including each LF, excluding the `K` line. `rows` is `1..8`, `cells == rows * 8`, and `n == cells`. The stream contains only scanned rows; the host expands to an 8x8 display with NaN for inactive rows. Invalid or rejected frames never enter the main store or plots.

Each capacitance integer is:

```text
rawFixed / 1,000,000 = pF
```

The invalid sentinel is:

```text
-1000000
```

The sentinel is detected before conversion and before applying the measurement-circuit correction. Valid values are corrected by:

```text
correctedPf = rawPf - 33.0
```

`FDC_CIRCUIT_OFFSET_PF = 33.0` is visible in setup. Negative corrected pF values are preserved. CSV/export metadata records canonical pF and the offset; display formatting never feeds back into calculation.

Absolute capacitance uses shared engineering units selected per heatmap or trend frame:

```text
abs(value) < 1,000 pF          -> pF
1,000 <= abs(value) < 1,000,000 -> nF
abs(value) >= 1,000,000        -> uF
```

Therefore `1,000,000 pF` displays as `1 uF`, not `1000 nF`. Delta mode is always `%`.

## Display Modes And Baseline

Modes:

- `Absolute C`: corrected capacitance.
- `Delta C/C0 %`: signed `((C - C0) / C0) * 100`.

Baseline capture is a timed 2 second session. Pressing `Capture Baseline`, or switching to delta mode without a valid baseline, starts collection from `t0` through `[t0, t0 + 2.0s)`. Only legal, complete, CRC-valid `CapacitanceFrame` events matching session generation, transport, device, active rows, firmware generation, request id, measurement domain, and circuit offset are used.

Each cell baseline is the median of valid corrected pF samples. A cell needs at least 3 samples. Inactive rows are not baselined. Near-zero `C0` is invalid to avoid divide-by-zero. Once complete, the baseline is frozen.

Baseline is invalidated by disconnect, reconnect, transport/device/session change, ROWS applied, firmware generation change, measurement domain change, circuit offset change, replay restart, clear all, parser reset, or user reset. Invalidation returns display to Absolute C and logs the reason.

## Four-point Selection

Clicking a heatmap cell selects exactly four cells on the same row and FDC:

- `D1..D4`: Primary FDC, for example `S3D2` selects `S3D1..S3D4`.
- `D5..D8`: Secondary FDC, for example `S5D7` selects `S5D5..S5D8`.

Cross-row selection and mixed primary/secondary groups are not supported. If ROWS shrinks and the selected row becomes inactive, the host corrects selection to the first active row and logs the correction.

## Transports

All inputs flow through:

```text
Transport bytes/datagram/notify
-> TransportEnvelope
-> raw recording/logging
-> reassembly/framing
-> ProtocolRegistry
-> typed domain event
-> bounded Store
-> immutable UI snapshot
-> Plotly/Dash rendering
```

The current application supports one main transport at a time:

- Serial: default `COM12`, `115200` baud, pyserial byte-chunk reader, auto reconnect option.
- Bluetooth LE: scans for `CscArray_` candidates and verifies service/characteristics before use.
- Wi-Fi UDP: discovers SoftAP candidates and confirms with CTRL protocol handshake.
- Replay: replays b41 ASCII, BLE/Wi-Fi envelope captures, legacy FAST_BINARY, MATV, and mixed startup logs.

BLE UUIDs:

```text
Service 00FF
CTRL_RX FF10 write
CTRL_TX FF11 notify/indicate
DATA_TX FF20 notify/indicate
LOG_TX  FF30 notify/indicate
```

BLE long messages use:

```text
G,<ch>,<mid>,<i>,<n>,<payloadLen>,<messageLen>,<messageCrc32>
<payload>
```

Fragments are reassembled by channel and message id, support out-of-order delivery, reject duplicate/missing/timeout/length/CRC failures, and only CRC-valid messages enter the protocol parser.

Wi-Fi is current firmware SoftAP + UDP only, not STA provisioning. Ports are:

```text
DATA 3333
LOG  3334
CTRL 3335
```

Device names are `CscArray_xxxxxx`; mDNS names are `cscarray-xxxxxx.local`. The default `192.168.4.1` is a candidate, not the only discovery method. Discovery uses SSID hints, mDNS/default candidate, optional bounded subnet probing, and CTRL handshake such as `BAT?`, `RAIL?`, or `ADS?`.

## ROWS Control

ROWS commands go through `CommandService`:

```text
ROWS=1
...
ROWS=8
```

`RCMD` means accepted and UI shows pending. Only `RAPP` means applied; only then does Active Rows update and baseline invalidate. The following C frame must match `rows == RAPP.new` and `gen == RAPP.gen`; mismatches are logged as protocol errors.

## Battery Card

Battery logs parsed include `AB50`, `ABAT`, `BATD`, `ARL`, and `ADS`. The UI shows battery voltage, validity/stale/unknown state, reason, freshness, age, rail state, and ADS chip/connection state.

When `bt=-1`, the host displays `N/A` or invalid, never `-1 mV`. No battery percentage is fabricated. Reasons include `adc_timeout`, `adc_stale`, `adc_status_error`, `rail_invalid`, `reference_invalid`, `absent_or_open`, `range_error`, and `unknown`.

Buttons send:

- Refresh: `BAT?`
- Diagnose: `BATD`
- Rail: `RAIL?`
- ADS: `ADS?`

## Raw Logs

Raw Logs is a bounded ring terminal, default 10,000 lines. It records Serial raw lines, BLE DATA/LOG/CTRL, BLE fragment diagnostics, Wi-Fi DATA/LOG/CTRL, Replay, Host, Discovery, Parser, CRC, Commands, RCMD, RAPP, ACK, ERR, SF50, TR50, AB50, OT50, BL50, I2C50, T50, ROW50, FB50, P50, H50, HC, BATD, ARL, ADS, RST, and unknown logs.

C/D/K DATA can be hidden in the UI to avoid flooding, but it is not discarded and remains available to raw recording/export.

## Legacy Compatibility

Legacy protocols are retained as independent plugins:

- `SAC1` / `FAST_BINARY` compact voltage frame.
- Fixed 312 byte little-endian frame.
- CRC32, resync, validMask, dropped/decimated counters, and ASCII-after-binary diagnostics.
- 64 int32 microvolts canonical unit.
- `MATV`, `MATV_RAW`, `MATV_GAIN`, `MATV_ERR`.
- Text voltage logs.
- Resistance input when logs provide direct ohm values.
- CSV export and replay.

FAST_BINARY is never treated as the b41 capacitance protocol.

## Install

Use this repository `.venv`, not ESP-IDF Python:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Run:

```powershell
.\.venv\Scripts\python.exe -m sensorarray_app
```

Open:

```text
http://127.0.0.1:8050
```

Compatibility wrapper:

```powershell
.\.venv\Scripts\python.exe main.py
```

## Test And Check

```powershell
.\.venv\Scripts\python.exe -m compileall src tests
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pytest -q
```

Replay fixtures are under `tests/fixtures/b41`, `tests/fixtures/legacy_binary`, and `tests/fixtures/legacy_matv`. Fixture CRCs are generated by `tests/fixtures/generate_b41_fixtures.py`.

## Performance And Bounds

The design keeps acquisition and rendering decoupled:

- bounded raw input queue;
- drop-oldest/keep-latest behavior for slow consumers;
- bounded raw log ring;
- numpy matrix history ring;
- display downsampling without mutating export history;
- parser/store/UI snapshot separation;
- render-skip counters that mean UI display coalescing, not device data loss.

Tracked counters include transport bytes/packets, parser frames/rejects, CRC failures, sequence gaps, fragment drops, host queue drops, history overwrites, render skipped, visual FPS, parser FPS, and stored FPS.

## Architecture

See `docs/architecture.md` for module ownership, worker boundaries, data flow, protocol registry, queue/backpressure policy, baseline invalidation, snapshot revision, Plotly update strategy, and migration map.

## Troubleshooting

- `COM12 occupied`: close the other program; validation must not silently switch to another COM port.
- `BLE not found`: run Scan once more; the app does not hard-code MAC addresses.
- `BLE service incomplete`: the candidate is listed but not selected as a verified SensorArray.
- `Wi-Fi SSID found but not connected`: connect Windows to the SoftAP or provide a reachable host fallback; the app must not claim UDP connected only from SSID presence.
- `mDNS failure`: default SoftAP host and bounded subnet candidates are still attempted.
- `Windows Firewall`: allow UDP 3333/3334/3335 for DATA/LOG/CTRL.
- `CRC failure`: rejected frame is kept in Raw Logs/diagnostics and is not stored.
- `baseline invalid`: recapture after the listed invalidation reason.
- `bt=-1`: battery is invalid or unavailable; no percent is shown.

## Known Limits

Hardware validation still depends on a connected device. If COM12, BLE, or Wi-Fi are unavailable, reports must state the real blocker and must not substitute replay/mock results for hardware validation. The current host does not implement Wi-Fi STA provisioning because b41 firmware exposes SoftAP + UDP.
