# SensorArray Desktop Host

SensorArray Desktop Host is the Electron/React/FastAPI application for the
SensorArray 8x8 measurement matrix. It preserves the existing capacitance path
and adds two ways to select the measurement quantity:

- `MODE=CAP|VOLT|RES` is the backwards-compatible quick action that sets all
  eight row modes at one frame boundary.
- `ROWMODES=<8 characters>` atomically applies an eight-row profile, where
  `C`, `V`, and `R` mean capacitance, voltage, and resistance.

For example, `ROWMODES=RVVCCVVR` configures S1/S8 as RES, S2/S3/S6/S7 as
VOLT, and S4/S5 as CAP. A homogeneous saved eight-row profile continues to use
the established single-quantity frame families. Firmware `8045e9e9` also emits
the atomic `M/MR/K` family for mixed-row acquisition. The host accepts a
homogeneous active prefix in an `M` frame and does not invent a heterogeneity
requirement that is absent from the wire contract.

Serial, Bluetooth LE, Wi-Fi UDP, and Replay all feed the same content-routed
protocol layer, typed command/domain state, stores, WebSocket snapshot, and
React UI. Replay validates that software path; it is not evidence that a real
Serial, BLE, or Wi-Fi link passed.

## Firmware authority and compatibility status

The sibling [SensorArray firmware repository](https://github.com/ErrorChen/SensorArray)
is authoritative for production wire bytes. Formatter and command code plus
their tests outrank copied host fixtures and prose.

This host upgrade targets exact firmware commit
`8045e9e9ec9599533c52c15dfcb6002f79fd15f1`, which provides:

- `ROWMODES?`, `ROWMODES=...`, `RMACK`, `RMAPP`, and `RMERR`;
- heterogeneous `M/MR/K` measurement frames;
- `MAPP` and `RMAPP` publication on BLE LOG `FF30`;
- automatic internal ADS analogue rail-span telemetry.

The Python mixed assembler and Replay fixtures follow the exact formatter in
`main/output/sensorarrayTextProtocol.c`: short identity keys
`rgen/rrid/pgen/prid`, `MR,s=...,m=CAP|VOLT|RES`, comma-separated `D=` values, and CRC
over the exact `M` and `MR` lines including LF.

Firmware `8045e9e9` completes both heterogeneous and homogeneous
`ROWMODES=CCCCCCCC|VVVVVVVV|RRRRRRRR` through strict `RMACK` followed by one
terminal `RMAPP` or `RMERR`. Data frames are never used as an applied-event
substitute; request-ID correlation remains mandatory.

See [measurement protocol compatibility notes](docs/measurement-protocol.md)
for the exact target schema and the firmware evidence boundary.

## Protocol summary

Existing homogeneous formats remain supported:

- CAP: `C` header, `D` chunks, and `K` CRC trailer.
- VOLT: `V` header, `D` value/error chunks, `P` PGA chunks, and `K`.
- RES: `R` header, `D` value/error chunks, `P` PGA chunks, and `K`.

VOLT data is signed integer microvolts (`unit=V,scale=-6`); RES data is
integer milliohms (`unit=ohm,scale=-3`). `Xhh` carries a firmware cell error
and is never converted to zero. `PGA=00` means verified bypass.

Mode and row-profile transactions are independent and strict:

```text
MODE=RES
MACK,id=41,old=CAP,new=RES,state=accepted
MAPP,id=41,gen=7,old=CAP,new=RES,seq=120,state=applied,...

ROWMODES=RVVCCVVR
RMACK,id=62,old=CCCCCCCC,new=RVVCCVVR,state=accepted
RMAPP,id=62,gen=11,seq=201,profile=RVVCCVVR,state=applied
```

`MACK` and `RMACK` mean queued/accepted only. The host keeps the last applied
state until the matching `MAPP` or `RMAPP` supplies the generation and frame
boundary. A wrong-ID applied event is rejected. A data frame never completes a
pending transaction; timeout or `MERR`/`RMERR` exposes an error without
fabricating application.

On BLE the existing single client subscribes once to CTRL TX `FF11`, DATA
`FF20`, and LOG `FF30`. Fragment reassembly precedes content routing. `MACK` or
`RMACK` may arrive through `FF11`; `MAPP` or `RMAPP` must arrive through
`FF30`, then traverse `BleTransport -> BleFragmentReassembler ->
ProtocolRegistry -> TextLogProtocol -> CommandService`. There is no second BLE
client, duplicate `FF30` subscription, UI-side parser, or data-frame shortcut.

## Install and run

Use the repository virtual environment for Python and the system Node.js/npm
installation for Electron:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

cd desktop
npm.cmd install
npm.cmd run desktop
```

Electron starts the Python backend sidecar, probes loopback ports `8888`
through `8988`, waits for `GET /health`, and supplies the selected backend URL
through the preload bridge. Backend-only development remains available:

```powershell
.\.venv\Scripts\python.exe -m sensorarray_backend --host 127.0.0.1 --port 8888
```

## Architecture

```text
Serial / BLE / Wi-Fi / Replay
             |
             v
      ProtocolRegistry
             |
  C/V/R parser | mixed M/MR/K parser | TextLogProtocol
             |
    typed frames and command transactions
             |
 MatrixStore / history / telemetry / CommandService
             |
        FastAPI + WebSocket
             |
         React + ECharts
```

The logical backing geometry remains 8x8. `frame.rows` is the active geometry
authority and can be any integer from 1 through 8. Canonical `8045e9e9` mixed
rows keep explicit row mode, unit, scale, validity, freshness, error, `fmt`, and
eight fixed-point/error tokens. The store also keeps separate CAP, VOLT, and RES value caches so
an ohm value cannot later be interpreted as pF. A row mode change starts a new
visible trend segment rather than drawing a line across incompatible units.

## Measurement and row-mode API

Key measurement endpoints are:

```text
GET  /api/measurement/mode
POST /api/measurement/mode
GET  /api/measurement/row-modes
POST /api/measurement/row-modes
POST /api/measurement/rail       deprecated/debug compatibility only
POST /api/rows
```

The row-mode POST body is typed and always has eight entries:

```json
{
  "modes": ["RES", "VOLT", "VOLT", "CAP", "CAP", "VOLT", "VOLT", "RES"]
}
```

The backend encodes that request once as `ROWMODES=RVVCCVVR`. It never emits
eight separate row commands. The snapshot exposes global and profile
transactions separately, including applied/pending modes, state, error,
request ID, generation, and boundary sequence.

Setup profiles store `acquisition.rows` in the full `1..8` range and
`acquisition.rowModes` as exactly eight modes. An older profile without
`rowModes` is migrated by repeating its legacy `measurementMode` (or CAP) for
all rows. Legacy `voltageRail` values remain readable for compatibility but
are not sent by the normal setup or measurement workflow.

## Desktop behavior

The Measurement Mode panel provides:

- **Set all rows: CAP | VOLT | RES**, using the legacy `MODE` transaction;
- eight S1-S8 row selectors with a draft profile and one **Apply row modes**
  button;
- distinct applied profile, pending profile, transaction state, and error;
- a read-only **ADS analogue rail span** readout.

All eight row selectors remain visible when `ROWS < 8`. Rows outside the active
geometry are dimmed and labelled `Inactive with current ROWS setting`; their
saved profile is retained for later use.

The heatmap renders only S1 through S`frame.rows`, so `ROWS=1` is a 1x8 matrix
without ghost rows. A mixed matrix remains one physical Nx8 heatmap, but CAP,
VOLT, and RES are separate ECharts series with separate colour scales and
units. Axis labels and tooltips include the row mode. CAP Delta C/C0 affects
only CAP rows; VOLT and RES rows remain absolute.

Backend colour ranges are authoritative and isolated as `cap_absolute`,
`cap_delta`, `voltage`, and `resistance`. Only finite, valid, fresh, non-error
cells in active rows participate. Frozen and last-good ranges are stored per
domain. When usable data has no span, the deterministic fallback is:

- positive CAP/RES: zero to at least `value * 1.05`;
- VOLT and CAP delta: a zero-centred symmetric extent.

This fixes the single-value midpoint-white bug without a `ROWS==1` colour
special case.

## Rail and battery telemetry

Normal VOLT and row-profile requests no longer depend on `RAILCFG`. Firmware
owns the internal rail monitor, and the UI displays only the measured
`AVDD - AVSS` span, validity, freshness/age, source, reason, and timestamp.
Unknown telemetry is shown as **Rail unavailable**. The legacy rail endpoint
and parser remain available only for explicit debug/raw-command compatibility.

Battery state is also backend-owned. `TelemetryStore` keeps the latest attempt
and the last known good voltage separately. Firmware-provided last-good fields
take priority; older firmware falls back to a valid measurement from the same
host device session. An invalid later attempt changes the status/reason but
does not erase the voltage. A device identity change clears the cache, while a
temporary reconnect to the same device retains it as stale.

## Validation

Run the software gates from the repository root:

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

Electron Replay E2E must use the built local renderer, real Electron preload,
Python sidecar, REST/WebSocket path, and retained screenshots. It must not be
reported as BLE HIL.

Real-hardware acceptance requires the full Electron application and the exact
firmware artifact under test. Each CAP, VOLT, RES, and
`RVVCCVVR` mixed run is 120 seconds. Switching stress includes at least ten
cycles of `CAP -> RES -> VOLT -> RVVCCVVR -> CCCCCCCC -> RRRRRRRR ->
VVVVVVVV`. BLE acceptance must observe accepted responses on `FF11`
and matching applied events on `FF30`; Serial is not an applied-event sidecar.
Homogeneous and heterogeneous `ROWMODES` terminals must both be observed and
reported from the real transport. BLE reconnect acceptance forces 30
unexpected disconnects and requires automatic same-device resubscription and
bootstrap on every connection generation. The Serial scientific-recorder
endurance interval is 450 seconds (7.5 minutes). Its sequence audit is bounded
by the opening and closing `PERF.frames` watermarks; frames beyond the closing
watermark are a recorded live tail, not silently assigned to that closed
interval. Replay remains software evidence only.
See [validation](docs/validation.md) for the complete evidence checklist and
required screenshots.

## Windows packaging

From the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\package_windows.ps1
```

Artifacts are written under `desktop/release/`. Smoke-test
`desktop/release/win-unpacked/SensorArray.exe` before distributing the NSIS
installer. The packaged application uses the PyInstaller backend sidecar under
`process.resourcesPath\backend`; end users do not need Python, Node.js, npm, or
the source tree.

```powershell
cd desktop
npm.cmd run test:packaged
```
