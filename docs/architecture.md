# SensorArray Desktop Architecture

## Ownership

```text
desktop/electron      Electron lifecycle, backend child process, preload, dialogs
desktop/src           React controls, typed REST/WebSocket state, ECharts
src/sensorarray_backend
  api/                FastAPI routes and WebSocket endpoint
  core/               runtime wrapper, snapshots, colour policy, history view
src/sensorarray_app
  domain/             typed frames, row profiles, battery and rail telemetry
  protocol/           C/V/R, mixed M/MR/K, text logs, BLE fragments, legacy plugins
  transport/          Serial, BLE notify, Wi-Fi UDP, Replay, discovery
  store/              matrix/domain caches, history, logs, telemetry, statistics
  services/           command transactions and discovery
```

Firmware owns production wire syntax and physical acquisition. Python owns
protocol parsing, transaction correlation, typed state, and display policy.
React consumes typed REST/WebSocket contracts; it never parses raw firmware
lines or creates a parallel view of applied state.

## Data flow

```text
Device or Replay
 -> transport envelope
 -> BLE fragment reassembly when applicable
 -> ProtocolRegistry content routing
 -> C/D/K or V|R/D/P/K assembler
    | M/MR/K mixed assembler
    | TextLogProtocol
 -> typed frame / telemetry / CommandTransactionEvent
 -> CommandService + MatrixStore + HistoryStore + TelemetryStore
 -> backend snapshot and domain-specific colour ranges
 -> REST / WebSocket
 -> React state
 -> one physical Nx8 ECharts heatmap + trend views
```

Replay enters at the same transport-envelope boundary and follows the same
parser/store/snapshot/UI path. It proves that path only; it cannot prove that a
firmware formatter or BLE notification path produced the fixture bytes.

## Transport contracts

- One active transport and one device session at a time.
- Session generation changes on transport switch, new device, and Replay
  restart. Late events from old sessions are ignored.
- BLE uses one client with existing `FF11` CTRL TX, `FF20` DATA, and `FF30` LOG
  subscriptions. Fragment reassembly precedes registry routing.
- `MACK`/`RMACK` from `FF11` and `MAPP`/`RMAPP` from `FF30` converge at
  `TextLogProtocol` and `CommandService`; there is no transport-specific
  applied-event parser.
- Wi-Fi DATA/LOG/CTRL stay separate UDP channels.

## Independent transaction state machines

Transport, global measurement mode, active ROWS, and row-mode profile are
separate concerns:

```text
connection.transportMode        serial | ble | wifi | replay
measurement.appliedMode         CAP | VOLT | RES
measurement.pendingMode         CAP | VOLT | RES | null
measurement.rowProfile          independent eight-row transaction state
frame.rows                      active physical geometry, 1..8
```

Global `MODE=...` remains the backwards-compatible set-all-rows action. It uses
`MACK` as accepted/queued and commits only matching `MAPP`. The row-profile
state machine emits exactly one `ROWMODES=<8 chars>` request, treats `RMACK` as
accepted, and commits only matching `RMAPP`. Each state machine has its own
request ID, generation, frame-boundary sequence, transition state, timeout, and
error.

Neither a homogeneous nor mixed data frame can complete a pending command.
First-attach synchronization from a complete frame is allowed only when no
corresponding transaction is pending. This preserves strict correlation while
still allowing attachment to an already streaming device.

## Typed measurement model

Established `CapacitanceFrame` and homogeneous VOLT/RES `MeasurementFrame`
models remain intact. Mixed frames are not flattened into a homogeneous frame
with `unit="mixed"`.

```text
MixedMeasurementFrame
  seq, timestampUs, rows, cells
  rowsGeneration, rowsRequestId
  profileGeneration, profileRequestId
  profile[8]
  rowFrames[activeRows]

RowMeasurement
  physical row identity
  mode, unit, scale
  raw fixed values[8], physical values[8]
  valid/fresh/error masks and error codes[8]
  canonical fmt plus optional legacy Replay diagnostics
```

`MixedMeasurementAsciiParser` emits this model only after one unique ascending
row record exists for every active physical row, row modes match the profile,
identities agree with the trailer, and CRC succeeds. Partial frames never
reach MatrixStore.

The parser follows the production formatter at firmware
`8045e9e9ec9599533c52c15dfcb6002f79fd15f1`: `M` and `K` use
`rgen/rrid/pgen/prid`; `MR` uses one-based `s`, `m=CAP|VOLT|RES`, canonical
unit/scale/format, and eight comma-separated `D=` tokens. The complete saved
profile, not only the active prefix, determines whether firmware emits mixed
frames. Long-key/pipe-separated records remain a Replay compatibility alias
only.

The same firmware emits terminal `RMAPP`/`RMERR` events for both homogeneous
and heterogeneous `ROWMODES` branches. CommandService completes a profile
transaction only from the matching terminal event; a legacy data frame is
never used as a substitute acknowledgement.

## Integrity accounting and serial wire recovery

Physical sequence spacing is partitioned into intentional USB DEBUG
decimation, firmware non-fresh suppression, source-specific firmware transport
drops, Host ingress drops, and an explicitly unexplained remainder. SF50's
`drop=0/<text-bus>/<all-sinks>` aggregate remains health evidence and is not
allowed to claim that one particular transport lost a measurement. PERF
`usbDrop` and BLE `BL50.dropD` establish per-source cumulative baselines on
attach; later deltas may reconcile sequence gaps only after SF50/PERF
non-fresh evidence has had priority.

The ESP USB CDC output can interleave diagnostic `printf` bytes with a queued
measurement packet. ProtocolRegistry may recover only an exact embedded
8045 `C`, `V`, `R`, or `M` header carrying the complete identity/geometry
signature. It emits a typed `WIRE_INTERLEAVE` warning and discards an existing
partial frame if necessary. It never repairs values, invents rows, fills zeros,
or bypasses the normal CRC and identity checks.

## Matrix and history storage

The backing geometry always remains 8x8. `activeRows` controls visibility and
eligibility, not allocation. Each snapshot carries:

```text
layout                 HOMOGENEOUS | MIXED
rowModes[8]
rowUnits[8]
rowScales[8]
capValues[8][8]
voltValues[8][8]
resValues[8][8]
current values[8][8]
```

On a mixed commit, each active row updates only its matching domain cache. A
RES row therefore cannot overwrite CAP cache data that might be used after the
row returns to CAP. The current display matrix selects the cache represented by
each current row record, while inactive rows serialize as null.

History records row mode/unit/scale for every sample. Queries for the selected
cell and current mode mask samples from other modes to NaN/invalid. That mask
creates an intentional discontinuity, so a resistance value is never connected
to a later capacitance value on one trend line.

CAP offsets and baseline are applied only to rows currently in CAP. In mixed
Delta C/C0 mode, CAP rows use percent while VOLT/RES rows remain absolute.

## Snapshot contract

The WebSocket snapshot exposes global transaction, row-profile transaction,
frame identity, row semantics, and typed telemetry separately. Central fields
include:

```text
measurement.appliedMode
measurement.pendingMode
measurement.transitionState
measurement.requestId
measurement.generation
measurement.frameSeq
measurement.rowProfile.appliedModes[8]
measurement.rowProfile.pendingModes[8] | null
measurement.rowProfile.transitionState
measurement.rowProfile.requestId
measurement.rowProfile.generation
measurement.rowProfile.frameSeq
measurement.rowProfile.error
measurement.railTelemetry

frame.rows
frame.layout
frame.rowModes[8]
frame.profileGeneration
frame.profileRequestId

matrix.values[8][8]
matrix.valid/fresh/error masks
matrix.modeByRow[8]
matrix.unitByRow[8]
matrix.scaleByRow[8]
matrix.capValues/voltValues/resValues

display.colourRanges.cap_absolute
display.colourRanges.cap_delta
display.colourRanges.voltage
display.colourRanges.resistance
```

Generic homogeneous compatibility fields remain available. In mixed layout,
`modeByRow`, `unitByRow`, and `scaleByRow` are authoritative; consumers must not
infer one unit from a generic matrix field.

## Authoritative colour policy

Python computes colour ranges so Replay, REST clients, and Electron share one
policy. The usable mask is explicitly:

```text
row < activeRows
AND valid
AND fresh
AND NOT error
AND finite
```

Ranges and freeze/last-good caches are independent for `cap_absolute`,
`cap_delta`, `voltage`, and `resistance`. For each domain the precedence is:

1. its frozen range;
2. its current nondegenerate usable range;
3. its last nondegenerate range;
4. a deterministic cold-start fallback.

For positive CAP absolute and resistance singleton/equal values, the fallback
starts at zero and extends to at least `value * 1.05`. For signed voltage and
CAP delta it is symmetric about zero. This is based on absence of dynamic span,
not `ROWS==1`, so a single 10025-ohm or 670-ohm value is placed above the
neutral midpoint rather than at white.

The frontend normally consumes these ranges. Its fallback exists only for
legacy Replay/snapshots that lack typed `colourRanges`.

## Heatmap architecture

One Nx8 physical grid is rendered, where `N=clamp(frame.rows,1,8)`. Title,
axis labels, data, invalid targets, selection overlay, pointer hit testing,
hover coordinates, keyboard movement, and CSS grid geometry all use that same
active row count.

Mixed layout creates mode-specific ECharts series and visual maps:

- CAP series: pF or CAP delta %;
- VOLT series: V;
- RES series: ohm.

Each series contains only rows of that mode and uses only its corresponding
backend range. Legends include only modes present in active rows, while row
labels identify both physical row and mode. This preserves one physical matrix
without comparing pF, V, and ohm in one min/max domain.

Selection coordinates may survive a row-mode change, but tooltip mode/unit
comes from the new row semantics. A selection outside a newly reduced ROWS
geometry is cleared or clamped before rendering.

## Rail and battery telemetry ownership

Rail telemetry is typed backend state containing span in uV, validity,
freshness/age, source, reason, and timestamp. It is read-only in the normal UI.
The normal global or row-profile VOLT path never emits `RAILCFG`; the retained
rail transaction is deprecated debug compatibility.

Battery telemetry is not cached in React. `TelemetryStore` owns:

```text
latestBatteryAttempt
lastGoodBattery
deviceIdentity
```

Firmware last-good fields take priority. Older firmware uses a same-device,
same-session host fallback. A failed latest attempt changes state/reason without
erasing last-good voltage. Changing device identity clears battery and rail
telemetry so one board's value cannot appear for another; a short reconnect to
the same device retains it as stale.

## Profile migration

Setup profiles require `acquisition.rows` in `1..8` and
`acquisition.rowModes` with exactly eight typed modes. When an older profile has
no `rowModes`, migration repeats its legacy `measurementMode`, falling back to
CAP. Legacy `voltageRail` fields remain readable and round-trippable but are
not displayed or transmitted by the production measurement workflow.
