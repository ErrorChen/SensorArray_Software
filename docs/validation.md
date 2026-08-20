# Validation

Validation results are reported in separate evidence classes. Success at a
lower layer cannot be promoted to a higher one:

1. Python protocol/domain/store/runtime/API tests;
2. frontend typecheck, lint, unit tests, and production build;
3. Electron Replay E2E through the real backend and WebSocket;
4. Electron/preload/backend-sidecar smoke;
5. Serial hardware GUI acceptance;
6. BLE hardware GUI acceptance;
7. optional Wi-Fi hardware GUI smoke;
8. packaged `win-unpacked` smoke.

Replay PASS is not Serial/BLE/Wi-Fi PASS. A Python build or browser component
test is not Electron GUI acceptance. A firmware build is not wire/HIL evidence.
Unavailable hardware or missing firmware capability is reported as
`BLOCKED / NOT RUN`, never inferred from fixtures.

## Firmware precondition

The authoritative firmware revision is
`8045e9e9ec9599533c52c15dfcb6002f79fd15f1`. Validate its production C
formatter, not stale copied examples. Canonical mixed wire uses
`rgen/rrid/pgen/prid`, `MR,s=<row>,m=CAP|VOLT|RES`, canonical `fmt`, comma-separated
`D=` values, and the short-key `K` trailer. CRC covers exact `M` plus ordered
`MR` bytes including LF.

Firmware `8045e9e9` supplies terminal row-profile events for homogeneous and
heterogeneous profiles. The Host must still reject a missing/wrong terminal
and must not use C/V/R data as an applied event. Report these separately:

```text
8045e9e9 exact mixed parser compatibility: software fixture/source PASS or FAIL
Heterogeneous BLE RMACK/RMAPP and mixed frame HIL: measured PASS/FAIL/BLOCKED
Homogeneous ROWMODES terminal transaction: measured PASS/FAIL/BLOCKED
Switching stress containing homogeneous ROWMODES: measured PASS/FAIL/BLOCKED
```

Do not use Serial as a hidden applied-event source for a BLE PASS.

## Software gates

Run from the repository root using the repository virtual environment:

```powershell
.\.venv\Scripts\python.exe -m compileall src tests scripts
.\.venv\Scripts\python.exe -m pytest -q

cd desktop
npm.cmd install
npm.cmd run typecheck
npm.cmd run lint
npm.cmd run test
npm.cmd run build
npm.cmd run test:e2e
```

Report the command, exit code, and test count for every gate. If installation
or a command was not run, state that explicitly rather than calling the
remaining subset a full pass.

## Python acceptance matrix

The Python suite must cover at least:

### BLE and command transactions

- one FF11 message containing MACK and fragmented FF11 MACK;
- fragmented FF30 MAPP through `BleTransport -> BleFragmentReassembler ->
  ProtocolRegistry -> TextLogProtocol -> CommandService`;
- matching request ID/generation/sequence commit and pending clear;
- wrong-ID MAPP rejection;
- RMACK, RMAPP, and RMERR typed parsing;
- one atomic `ROWMODES=<8 chars>` send;
- row-profile requested -> accepted -> applied state;
- wrong-ID/profile RMAPP rejection, RMERR revert/error, and timeout;
- data-frame observation never completing a pending MODE or ROWMODES request;
- normal `MODE=VOLT` emitting no `RAILCFG`;
- a VOLT-containing row profile emitting only `ROWMODES=...`.

### Frame parsers and stores

- established C/D/K and V|R/D/P/K CAP/VOLT/RES regressions;
- signed VOLT, all PGA values including bypass, `Xhh`, independent masks, and
  CRC including P records;
- valid mixed M/MR/K CRC and full typed row identity;
- incomplete frame, duplicate row, missing/reordered row, profile mismatch,
  trailer identity mismatch, and CRC failure rejection;
- no partial MatrixStore update from a rejected mixed frame;
- separate CAP/VOLT/RES caches and current row semantics;
- history discontinuity after row mode/unit changes;
- frame/snapshot layout, row modes, ROWS/profile generation and request IDs;
- setup and runtime ROWS acceptance for every integer 1 through 8.

### Colour, CAP, rail, and battery

- usable colour mask includes only active, finite, valid, fresh, non-error
  cells;
- single-value RES ranges for 10025 ohm and 670 ohm are nondegenerate and put
  the value above the midpoint;
- signed VOLT and CAP delta ranges are symmetric about zero;
- RVVCCVVR CAP values affect only CAP range, VOLT only VOLT range, and RES only
  RES range;
- frozen and last-good ranges are independent for CAP absolute, CAP delta,
  VOLT, and RES;
- mixed baseline/Delta affects only CAP rows;
- typed rail span validity/freshness/age/source/reason and unavailable state;
- firmware authoritative battery `lastGood*` fields plus legacy `bl*` aliases,
  older-firmware same-session
  fallback, invalid latest attempt preserving last-good voltage, next fresh
  update, and different-device reset;
- old setup profiles and legacy voltage-rail fields remain loadable without an
  automatic rail send.

Keep an exact formatter-source fixture that feeds canonical bytes through the
host parser and compares
sequence, geometry, identities, profile, row modes/units/scales, values, masks,
errors, formats, and CRC acceptance. Do not label a Host-generated Replay as
real BLE wire evidence.

## Frontend acceptance matrix

Unit/component tests must cover:

- Setup ROWS options 1, 2, 3, 4, 5, 6, 7, and 8;
- setup-profile round-trip/migration for all ROWS and eight row modes;
- eight RowModeProfile controls, inactive-row labels, draft editing, one Apply
  request, pending/applied separation, timeout/error, and retryable draft;
- Measurement Mode set-all actions and absence of AVDD/AVSS inputs;
- read-only rail fresh/stale/unavailable display;
- active heatmap title, axes, data, invalid layer, overlay, hit testing, hover,
  keyboard selection, and CSS geometry;
- one mixed Nx8 heatmap with CAP/VOLT/RES series and separate colour ranges;
- singleton/equal-value fallback without neutral midpoint white;
- CAP-only baseline/Delta behavior in mixed layout;
- selection invalidation when ROWS shrinks and unit refresh when row mode
  changes;
- tooltips containing SxDy, mode, value/unit, valid/fresh/error;
- Battery never-measured, fresh, stale last-known, failed last-known, and next
  fresh update.

## Electron Replay E2E

Formal GUI acceptance uses real Electron/Playwright with the locally built
`dist/index.html`. Electron starts the repository Python sidecar, and the
preload bridge supplies its dynamic loopback URL. Chrome, a standalone Vite
page, or a browser-only component test does not satisfy this gate.

Replay bytes traverse:

```text
Replay Transport
 -> ProtocolRegistry
 -> parser/TextLogProtocol
 -> typed domain event
 -> CommandService/MatrixStore/history/telemetry
 -> backend snapshot
 -> WebSocket
 -> React/ECharts
```

The Replay suite must exercise at least:

- legacy 8x8 CAP and homogeneous VOLT/RES compatibility;
- MACK-only pending without premature commit and matching MAPP application;
- RMACK/RMAPP plus mixed M/MR/K with `RVVCCVVR`;
- `ROWS=5` with `CRVCRVCR` and full eight-row saved profile;
- ROWS 1, 3, 5, and 8 visible geometry;
- independent pF/V/ohm colour scales and CAP Delta isolation;
- malformed/incomplete/bad-CRC recovery with no partial matrix;
- read-only rail telemetry and battery last-known failure state;
- no fatal console, backend, WebSocket, preload, or sidecar error.

Retain at least these screenshots under the validation artifact directory:

```text
rows1-res.png
rows3-cap.png
rows5-mixed.png
rows8-mixed.png
rail-readonly.png
battery-stale.png
```

Screenshot content requirements:

- `rows1-res.png`: title **1x8 Resistance Heatmap**, only S1 and D1-D8, a valid
  S1D1 resistance cell that is not neutral white, and no S2-S8 ghost row;
- `rows3-cap.png`: only S1-S3 with CAP units and geometry;
- `rows5-mixed.png`: five active rows, row-mode labels, and independent active
  scales from the saved profile `CRVCRVCR`;
- `rows8-mixed.png`: all eight rows and the acceptance profile `RVVCCVVR`;
- `rail-readonly.png`: no editable rail inputs and the internal span with
  freshness/source, or explicit unavailable state;
- `battery-stale.png`: numeric last-known voltage retained with stale/failure
  status.

These screenshots demonstrate rendered Replay states only. Their captions and
final report must not call them BLE hardware evidence.

## Hardware readiness

Before mode/profile commands after boot, wait for a complete CRC-valid, fully
fresh CAP frame. `ADSBOOT` is too early. Close other serial monitors and BLE
clients so they do not own the device.

Record at minimum:

- host baseline/final HEAD and dirty-state inventory;
- firmware commit/artifact hash and confirmation that it includes target
  ROWMODES/M/MR/K plus FF30 applied-event changes;
- device identity, transport, service/characteristics or COM port;
- timestamps, raw command/event excerpts, parser counters, disconnect/reset
  breadcrumbs, and screenshot paths;
- separate result for each evidence class.

The production VOLT flow has no AVDD/AVSS entry or automatic `RAILCFG`. The
firmware internal monitor must acquire the rail span, and the UI must display
that typed span read-only. Absence of rail telemetry is an explicit rail
failure/blocker, not a reason to invent values.

## Serial GUI acceptance

Use the full Electron GUI at 115200 baud:

1. Select and connect the real SensorArray port.
2. Confirm complete fresh CAP frames and strict MACK -> MAPP state.
3. Exercise every ROWS value 1 through 8 with matching ROWS transaction and
   geometry frame.
4. Exercise global CAP -> RES -> CAP and CAP -> VOLT -> CAP.
5. Apply `RVVCCVVR`; require matching RMACK/RMAPP and sustained mixed frames
   with correct profile generation/request ID/physical row.
6. Inspect heatmap labels/scales, tooltip units, CAP-only controls, rail
   telemetry, battery latest/last-good, raw log, and status.
7. Disconnect/reconnect and verify session/device isolation.

Before judging FULL ingest, issue `FPSCAP=OFF` and `OUTCAP=OFF`, then require
`FPS?` to report `cfcap=0,ofcap=0`. Bracket every sustained interval with
`PERF?`. For a fixed sequence interval, PASS requires every observed gap to be
partitioned into firmware non-fresh frames or source-specific firmware drops,
with zero new Host-unexplained gap, parser reject, CRC failure, and Host raw
queue overflow. Do not use SF50's aggregate all-sink drop count to hide a
source-specific loss.

`PERF.frames` is the causal sequence watermark for that reply. A gap above the
latest watermark is reported as `pendingFirmwareEvidenceGap`, not immediately
as Host loss, because the reply may have waited behind newer measurement
frames in the transport queue. Use the opening and closing `PERF.frames`
watermarks as the fixed interval boundaries. Frames already observed beyond
the closing watermark are an explicitly excluded live tail; a continuously
running FULL stream is not expected to let a queued reply catch its moving
latest-frame boundary.
`hostUnexplainedSequenceGap` contains only gaps at or below a covered firmware
watermark that are not explained by source-specific firmware counters. Never
associate a delayed `PERF` reply with the Host sequence that happened to be
current when the reply arrived.

Serial evidence is useful but cannot replace the BLE FF30 requirements below.

## BLE GUI and transaction acceptance

Use the GUI Scan -> verified SensorArray -> Connect flow. Verify service `00FF`
and the existing single subscriptions to CTRL TX `FF11`, DATA `FF20`, and LOG
`FF30`. Do not create a second client or subscribe to FF30 twice.

After the functional checks, force 30 unexpected BLE link drops while leaving
the GUI session active. Every cycle must automatically reconnect to the same
device, increment `connectionGeneration`, run bootstrap/resynchronisation, and
prove FF11/FF20/FF30 again with a strict mode transaction and a fresh data
frame. A manual Stop/Start is a separate lifecycle check and cannot substitute
for these unexpected-disconnect cycles.

### Global transaction

For CAP -> RES -> CAP through the GUI, capture:

```text
MACK from BLE CTRL FF11
MAPP from BLE LOG FF30
same request ID and expected mode
generation and frame-boundary sequence
UI requested -> accepted -> applied
```

The applied event must come from BLE. A RES/CAP data frame, longer timeout, or
Serial listener is not accepted as completion.

### Mixed transaction

Apply `RVVCCVVR`, then `CRVCRVCR`, each as one GUI action. Require:

- one transmitted `ROWMODES=<profile>`;
- RMACK on FF11 and matching RMAPP on FF30;
- correct request ID, profile generation, and boundary sequence;
- subsequent complete mixed frames with matching identities;
- correct S1-S8 physical row modes and independent units/scales;
- no partial update on fragment/parser failure.

### Rail and battery

From a fresh boot, confirm no rail input controls. Set all VOLT or apply a
profile containing VOLT, observe no host `RAILCFG`, and verify the firmware
internal `AVDD - AVSS` span is shown with correct fresh/stale/source state.

Wait for one fresh battery value, then observe a stale/invalid attempt. The
voltage must remain visible as last known with the failure reason and must
update on the next fresh sample. A different board identity must clear it.

For failures, retain connection state, discovered service/characteristics,
notify map, DATA/LOG/CTRL counts, fragment/parser statistics, first/last payload
prefix, matrix update count, backend exception, and reset breadcrumb. A generic
“BLE does not work” note is insufficient.

## Required 120-second stability

Use real Electron plus real BLE, not Replay. Run each state continuously for
120 seconds:

| Run | Required state |
| --- | --- |
| 1 | homogeneous CAP |
| 2 | homogeneous VOLT |
| 3 | homogeneous RES |
| 4 | mixed `RVVCCVVR` |

For every run record start/end status and counters. PASS requires no backend
crash, sidecar exit, BLE disconnect, firmware reset, unbounded memory growth,
unbounded React render loop, parser corruption, CRC runaway, stale profile,
unit crossover, or transaction desynchronization. The requirement is 120
seconds per state, not ten minutes and not a combined 120 seconds.

In addition, the Serial FULL scientific-recorder endurance gate runs for at
least 450 seconds (7.5 minutes). It requires `receivedFrames == writtenFrames`,
zero recorder drops, a clean finalised session, bounded renderer memory, and
the same parser/CRC/sequence-integrity conditions. The recorder gate is
independent of the four 120-second mode/profile runs.

## Switching stress

Run at least ten complete cycles of this exact global/profile sequence:

```text
CAP
RES
VOLT
RVVCCVVR
CCCCCCCC
RRRRRRRR
VVVVVVVV
```

Record every request ID, accepted event, applied event, generation, and
boundary sequence. A timeout, wrong-ID acceptance, or data-frame-inferred
application fails that cycle. Do not skip homogeneous entries or relabel a
partial heterogeneous subset as full stress PASS.

## Hardware test entry points

Build the desktop application first, then run each real-hardware phase from
`desktop/` with the real serial port in `SENSORARRAY_HIL_SERIAL_PORT`:

```text
node hardware-e2e/runHardwareHil.mjs --phase=serial-wire
node hardware-e2e/runHardwareHil.mjs --phase=serial
node hardware-e2e/runHardwareHil.mjs --phase=serial-switching
node hardware-e2e/runHardwareHil.mjs --phase=lifecycle
node hardware-e2e/runHardwareHil.mjs --phase=gui-stress
node hardware-e2e/runHardwareHil.mjs --phase=ble
node hardware-e2e/runHardwareHil.mjs --phase=ble-reconnect
node hardware-e2e/runHardwareHil.mjs --phase=mixed
node hardware-e2e/runHardwareHil.mjs --phase=wifi
```

`serial` includes the 450-second recorder endurance interval and the four
120-second state intervals. `serial-switching` executes ten complete cycles of
the required global/profile sequence. `lifecycle` verifies Recover, guarded
FDC enable/disable, restart, automatic reconnect, new boot identity, bootstrap,
and completion-after-reboot. `ble-reconnect` executes 30 forced link drops in
one Host session and one firmware boot; every new connection epoch must
bootstrap, complete a GUI ROWS transaction over FF11/FF30, and deliver a fresh
FF20 frame. Deliberate disconnect/bootstrap/configuration boundaries are
reported separately from the lossless 120-second sustained-run windows.
`gui-stress` executes 100 rounds comprising 400
tab changes, 100 resizes, and 100 minimise/restore cycles while lossless
recording remains active. Each phase writes its own JSON and screenshots under
`validation_artifacts/hardware`; one phase's PASS must not be promoted to
another phase. `wifi` performs only the bounded GUI discovery/confirmation
smoke and reports `BLOCKED` when the default SoftAP endpoint does not answer;
an unconfirmed fallback address is never counted as hardware PASS.

## Wi-Fi and packaging

When the network environment permits, verify DATA 3333, LOG 3334, CTRL 3335,
commands, matrix refresh, applied-event correlation, and disconnect through the
GUI. Report Wi-Fi separately from BLE and Serial.

For packaging:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\package_windows.ps1

cd desktop
npm.cmd run test:packaged
```

The packaged smoke launches `win-unpacked\SensorArray.exe`, requires an
`app.asar` local `file:` renderer, preload bridge, and healthy packaged Python
sidecar. If the NSIS installer was not produced/tested, report that separately.

After validation, close Electron, sidecars, development servers, and live
connections. Preserve screenshots and minimal failure evidence. The final
report must list each gate as PASS, FAIL, BLOCKED, or NOT RUN, with Replay and
hardware results in separate rows.
