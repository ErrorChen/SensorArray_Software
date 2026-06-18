# SensorArray Host Architecture

Baseline firmware commit:

```text
b41c5256fbb5b23a0f0d98ed651db2f6ced3a0d6
```

## Module Map

```text
main.py
src/sensorarray_app/
  app/            Dash bootstrap, runtime ownership, UI state
  domain/         typed events, capacitance math, baseline, units, selection, battery, voltage, resistance
  protocol/       C/D/K parser, CRC, BLE fragments, legacy FAST_BINARY, MATV, log parser, registry
  transport/      serial, BLE, Wi-Fi UDP, replay, discovery, session generation
  store/          matrix latest store, numpy history ring, raw log ring, telemetry, statistics
  services/       commands, discovery wrappers, CSV export, raw recording
  ui/             layout, figures, callbacks, assets
```

Compatibility files under `matrix_log_viewer/` remain for legacy parser tests and old imports. `matrix_log_viewer/run_viewer.py`, `matrix_log_viewer/run_gui.py`, and root `main.py` are thin wrappers into `sensorarray_app`.

## Data Flow

```text
Physical Transport
-> TransportEnvelope
-> Raw logging / optional recording
-> Reassembly
-> ProtocolRegistry
-> Domain Event
-> Store
-> Snapshot
-> UI rendering
```

TransportEnvelope fields include source, channel, deviceId, sessionGeneration, received monotonic time, received wall time, raw payload, remote address, and metadata.

Typed domain events include `CapacitanceFrame`, `VoltageFrame`, `ResistanceFrame`, `BatteryTelemetry`, `LogRecord`, `CommandAccepted`, `CommandApplied`, `TransportStateEvent`, `ParserErrorEvent`, and `DiagnosticSummary`.

## Worker Ownership

| Worker | Owns | Must not do |
|---|---|---|
| Serial reader | pyserial open/read/write, byte chunks, connection state | Plotly/Dash operations, protocol parsing in UI callbacks |
| BLE asyncio worker | GATT connect, notify subscription, FF10 writes, minimal notify copy | blocking UI work, heavy parsing in notify callback |
| Wi-Fi UDP workers | DATA 3333, LOG 3334, CTRL 3335 sockets | mix channels, block parser/store |
| Replay worker | bounded chunk replay and session restart | claim hardware validation |
| Parser worker | ProtocolRegistry, typed event commit, raw log commit | physical I/O blocking |
| UI callbacks | enqueue/connect/disconnect requests, read immutable snapshots, render figures | direct serial reads, BLE scans in callback thread, UDP blocking loops |
| Raw recorder | file writes from independent queue | block transport readers |

Session generation increments on connect, reconnect, transport/device switch, replay restart, and disconnect. Late packets from older sessions are ignored at manager/store boundaries.

## Transport

Serial uses pyserial and reads bytes, not `readline()`. Default validation port is `COM12` at `115200` baud.

BLE uses `bleak`. Discovery scans for `CscArray_` name hints and service UUID `00FF`, sorts by RSSI, and verifies `FF10/FF11/FF20/FF30` before selecting a target. Notifications are channel-specific. `G` fragments are reassembled by channel/message id and rejected on duplicate/missing/timeout/length/CRC failure.

Wi-Fi is SoftAP + UDP. DATA, LOG, and CTRL are separate sockets on 3333/3334/3335. Discovery combines SSID hints, mDNS/default candidate, optional bounded subnet probing, and CTRL handshake. `192.168.4.1` is a candidate only.

Replay emits the same envelopes as physical transports, with a fresh session generation on restart.

## ProtocolRegistry

Priority:

1. `SAC1` magic -> `LegacyFastBinaryVoltageProtocol`.
2. DATA channel C/D/K -> `B41CapAsciiProtocol`.
3. MATV text rows -> `LegacyMatvProtocol`.
4. Text/log rows -> `TextLogProtocol`.
5. Unknown lines -> Raw Logs.

This prevents `C,seq=...` from being split by the legacy text parser and prevents arbitrary binary bytes from becoming log records.

## C/D/K State Machine

```text
WAIT_HEADER
COLLECT_DATA
WAIT_TRAILER
VALIDATE
COMMIT_OR_REJECT
```

Rules enforced:

- `rows` is 1..8.
- `cells == rows * 8`.
- `n == cells`.
- D lines start at D0 and are continuous.
- Each D line has at most 16 values.
- Final D line may be short.
- Total values equal cells.
- K seq/gen/rid match C.
- CRC is reflected CRC-32 over exact C..D bytes with LF and without K.
- ASCII decoding is strict.
- New C before K rejects the previous pending frame.
- Pending timeout rejects the frame.
- Duplicate/missing/extra/short data are separately counted.

Only fully valid frames enter `MatrixStore`.

## Domain

Capacitance conversion:

```text
if rawFixed == -1000000:
    rawPf = NaN
    correctedPf = NaN
else:
    rawPf = rawFixed / 1_000_000.0
    correctedPf = rawPf - FDC_CIRCUIT_OFFSET_PF
```

Canonical capacitance is float64 pF. Engineering display units are chosen by one `EngineeringUnitFormatter` shared by heatmap, trend, tooltip, and export formatting.

Baseline is a 2 second `BaselineSession` with locked session generation, transport, device, active rows, firmware generation, request id, measurement domain, and circuit offset. It uses per-cell median and freezes the result. Invalidation returns the UI to Absolute C.

Selection stores row index, row label, FDC group, detector start/end, four cells, and revision. Primary is D1-D4; secondary is D5-D8.

Battery telemetry parses `AB50`, `ABAT`, `BATD`, `ARL`, and `ADS`. `bt=-1` becomes `None`; no SOC percentage is inferred.

Voltage and resistance are separate domains. FAST_BINARY and MATV stay in microvolts. Resistance is accepted only when logs provide direct ohm values because the legacy host did not contain a stable voltage-to-resistance formula.

## Store

`MatrixStore` keeps the latest 8x8 matrix and expands inactive rows to NaN. It does not fill missing cells from previous frames.

`MatrixHistoryStore` is a numpy ring with bounded frame capacity. Display downsampling reads from history without mutating it, so CSV export uses canonical history.

`RawLogStore` is a bounded ring, default 10,000 lines. File recording is separate and queue-backed.

`TelemetryStore` owns battery snapshots. `StatisticsStore` owns transport/parser/history/render counters.

## Snapshot And Rendering

The UI receives immutable snapshots through a Dash interval. Heatmap and trend are derived from the same snapshot revision where possible. Pause stops display updates only; transport, parsing, storage, and recording continue.

Plotly rendering uses stable graph IDs and `uirevision`. The included JS asset keeps a place for requestAnimationFrame coalescing and visual FPS metrics; server-side callbacks currently publish bounded figure payloads.

## Queue And Backpressure

- Raw input queue is bounded.
- Transport enqueue uses drop-oldest/keep-latest behavior where possible.
- Parser rejects do not enter stores.
- Raw log UI ring is bounded independently from raw recording.
- History overwrites are counted.
- `renderSkipped` means UI coalescing only, not device data loss.

## Reconnect And Baseline Invalidation

Reconnect or switching transports closes the previous transport, increments session generation, invalidates baseline, and clears stale worker callbacks. ROWS changes invalidate baseline only on `RAPP` applied.

## Migration Map

| Old file/class/function | Current function | New module | Action | Test coverage | Compatibility risk |
|---|---|---|---|---|---|
| `main.py` | root launcher | `sensorarray_app.__main__` | thin wrapper | app smoke | low |
| `run_viewer.py` | Dash launcher | `app.bootstrap` | thin wrapper | existing import/smoke | low |
| `run_gui.py` | Tk launcher | `app.bootstrap` | thin wrapper | compile | medium for old GUI users |
| `app.py` callbacks | UI/control/render | `ui/*`, `app/runtime.py` | replaced for new entry | UI smoke, legacy tests still import old | medium |
| `SensorArrayStreamParser` | SAC1 + startup text | `protocol/legacy_binary_voltage.py`, `protocol/registry.py` | retained as legacy | legacy parser tests | low |
| `binary_frame_parser.py` | FAST_BINARY voltage | `protocol/legacy_binary_voltage.py` | migrated plugin; old retained | legacy + new tests | low |
| `matv_parser.py` / `text_log_parser.py` | MATV/log voltage | `protocol/legacy_matv.py`, `log_protocol.py` | migrated plugin; old retained | legacy + new tests | low |
| `MatrixDataStore` | old voltage/MATV ring | `store/matrix_store.py`, `history_store.py` | replaced for new entry | store tests | medium |
| `ConnectionManager` | serial/replay only | `transport/manager.py` | replaced for new entry | runtime tests | medium |
| `serial_reader.py` | serial/replay thread | `serial_transport.py`, `replay_transport.py` | migrated | existing serial tests + compile | low |
| sample logs | legacy fixtures | `tests/fixtures/*` | classified | pytest | low |

## Test Strategy

Unit tests cover C/D/K rows and CRC, sentinel conversion, engineering unit thresholds, baseline median/window, selection, battery invalid handling, BLE fragment reassembly, matrix inactive row expansion, and Dash panel smoke.

Existing regression tests cover SAC1/FAST_BINARY CRC/resync/validMask, MATV parsing, old store behavior, render cache, status helpers, and legacy UI helpers.

Benchmarks remain in `matrix_log_viewer/benchmark_gui_pipeline.py`; future work should add benchmark assertions for parser/store/snapshot throughput and memory under sustained replay.
