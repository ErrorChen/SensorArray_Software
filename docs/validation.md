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

## Firmware precondition and current blocker

The authoritative firmware revision is
`331c44589318db9ba642cf3ab33bb08ca3dd8a34`. Validate its production C
formatter, not stale copied examples. Canonical mixed wire uses
`rgen/rrid/pgen/prid`, `MR,s=<row>,m=C|V|R`, canonical `fmt`, comma-separated
`D=` values, and the short-key `K` trailer. CRC covers exact `M` plus ordered
`MR` bytes including LF.

Source audit identifies one firmware-side acceptance blocker: homogeneous
`ROWMODES=CCCCCCCC|VVVVVVVV|RRRRRRRR` emits `RMACK`, then takes the legacy
frame path. In that path firmware `331c445` neither completes rowProfile nor
prints `RMAPP`/`RMERR`; those operations occur only inside the mixed branch.
The Host must time out strictly and must not use C/V/R data as an applied event.
Report this separately:

```text
331c445 exact mixed parser compatibility: software fixture/source PASS or FAIL
Heterogeneous BLE RMACK/RMAPP and mixed frame HIL: measured PASS/FAIL/BLOCKED
Homogeneous ROWMODES terminal transaction: BLOCKED (firmware defect)
Switching stress containing homogeneous ROWMODES: FAIL/BLOCKED at that step
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
5. Apply `RVVCCVVR` and `CRVCRVCR`; require matching RMACK/RMAPP and sustained
   mixed frames with correct profile generation/request ID/physical row.
6. Inspect heatmap labels/scales, tooltip units, CAP-only controls, rail
   telemetry, battery latest/last-good, raw log, and status.
7. Disconnect/reconnect and verify session/device isolation.

Serial evidence is useful but cannot replace the BLE FF30 requirements below.

## BLE GUI and transaction acceptance

Use the GUI Scan -> verified SensorArray -> Connect flow. Verify service `00FF`
and the existing single subscriptions to CTRL TX `FF11`, DATA `FF20`, and LOG
`FF30`. Do not create a second client or subscribe to FF30 twice.

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

## Switching stress

Run at least ten complete cycles of the global sequences:

```text
CAP -> RES -> CAP
CAP -> VOLT -> CAP
```

Also cycle these row profiles, requiring matching transaction completion each
time:

```text
CCCCCCCC
RVVCCVVR
VVVVVVVV
CRVCRVCR
RRRRRRRR
```

Record every request ID, accepted event, applied event, generation, and
boundary sequence. A timeout, wrong-ID acceptance, or data-frame-inferred
application fails that cycle. With exact firmware `331c445`, the homogeneous
entries are expected to expose the known missing-terminal-event defect; do not
skip them or relabel the partial heterogeneous subset as full stress PASS.

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
