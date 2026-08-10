# SensorArray Desktop Host

SensorArray Desktop Host is the Electron/React/FastAPI application for the
SensorArray 8x8 measurement matrix. It supports three measurement quantities
without replacing the existing transport and desktop architecture:

- `CAP`: capacitance in pF, including circuit correction, per-cell offsets,
  baseline capture, and Delta C/C0 %.
- `VOLT`: signed fixed-point microvolts converted to volts.
- `RES`: fixed-point milliohms converted to ohms.

Serial, Bluetooth LE, Wi-Fi UDP, and Replay all feed the same content-routed
protocol layer, typed measurement state, WebSocket snapshot, heatmap, and trend
charts. Replay validates the software path; it is not evidence that a hardware
transport passed.

## Protocol authority

The sibling [SensorArray firmware repository](https://github.com/ErrorChen/SensorArray)
is authoritative for the measurement and command wire protocol. Host fixtures
are compatibility copies, not a second protocol specification. When firmware
documentation disagrees with production formatter/command code and its tests,
the implementation and tests take precedence.

The current host understands:

- CAP `C` headers, `D` value chunks, and `K` CRC trailers.
- VOLT/RES `V` or `R` headers, `D` value/error chunks, packed `P` PGA chunks,
  and `K` CRC trailers.
- `MODE?`, `STATE?`, `MODE=CAP|VOLT|RES`, and the strict `MACK` accepted / `MAPP`
  applied transaction.
- `ROWS`, `RCMD`, and `RAPP`.
- external measured-rail `RAILCFG`, `RACK`, `RAPP`, and `RERR`.
- ADS identity and `ADSCHK` diagnostics.
- battery cache, immediate transaction, scheduler, and diagnostic telemetry.

VOLT data is signed integer microvolts (`unit=V,scale=-6`); RES data is integer
milliohms (`unit=ohm,scale=-3`). A value such as `-1250` is a valid negative
voltage. `Xhh` is an invalid cell carrying firmware error code `0xHH`; it is
never converted to zero. PGA literals are `01/02/04/08/10/20` for x1 through
x32, while `00` means verified PGA bypass.

For the exact frame, mask, CRC, transaction, and diagnostic contracts, see
[Host measurement protocol compatibility notes](docs/measurement-protocol.md).

## Install and run

Use the repository virtual environment for Python and the system Node.js/npm
installation for Electron:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

cd desktop
npm.cmd install
npm.cmd run desktop
```

`npm.cmd run desktop` starts Vite and Electron. Electron starts the Python
backend sidecar, probes `127.0.0.1` ports `8888` through `8988`, waits for a
successful `GET /health`, and supplies the selected backend URL to the renderer
through the existing preload bridge. Backend-only development remains available:

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
       C / V / R parser
             |
   typed measurement + command events
             |
     MatrixStore / history / telemetry
             |
       FastAPI + WebSocket
             |
       React UI in Electron
```

The existing BLE service and characteristics remain `00FF`, `FF10`, `FF11`,
`FF20`, and `FF30`. Wi-Fi UDP remains DATA `3333`, LOG `3334`, and CTRL `3335`.
BLE fragmentation/reassembly occurs before content routing. A complete
measurement packet received on a log channel is still routed by its `C`, `V`,
or `R` content.

Transport mode and measurement mode are deliberately separate:

- `connection.transportMode`: `serial`, `ble`, `wifi`, or `replay`.
- `measurement.appliedMode`: `CAP`, `VOLT`, or `RES`.

The UI does not optimistically commit a measurement mode on `MACK`. It shows a
pending transition until a matching `MAPP` supplies the generation and frame
sequence boundary. Old-generation VOLT/RES frames and pre-boundary CAP frames
are rejected.

## Voltage rail configuration

VOLT requires a paired, externally measured rail snapshot from the current
power, wiring, and load condition:

```text
RAILCFG=<positive_AVDD_uV>,<negative_AVSS_uV>
```

Enter the readings as volts in Setup; the host converts them to integer uV. Do
not use nominal supply values, battery voltage, `RAIL?`, or the ADS supply
monitor as a substitute for an external DMM reading. The host applies
`RAILCFG` while in CAP/RES, waits for matching `RACK` and
`RAPP,source=external,state=applied`, and only then sends `MODE=VOLT`.

RES does not require this external rail workflow. CAP-only offset, baseline,
and Delta controls are not applied to VOLT or RES.

## API and snapshots

Key endpoints include:

```text
GET  /health
GET  /api/status
GET  /api/history

POST /api/transport/mode
GET  /api/transport/serial/ports
POST /api/transport/serial/connect
GET  /api/transport/ble/scan
POST /api/transport/ble/connect
GET  /api/transport/wifi/discover
POST /api/transport/wifi/connect
POST /api/transport/write
POST /api/transport/disconnect

GET  /api/measurement/mode
POST /api/measurement/mode
POST /api/measurement/rail
POST /api/rows

POST /api/replay/open
POST /api/replay/start
POST /api/replay/stop
POST /api/replay/seek

GET  /api/export/session?format=csv|xlsx|mat|h5
POST /api/import/session
GET  /api/setup/profile
POST /api/setup/profile
WS   /ws
```

Snapshots keep connection and measurement state distinct. Generic matrix data
includes quantity, unit, scale, values, raw fixed values, valid/fresh/error
masks, error codes, and PGA. CAP-specific raw/corrected/display pF, offsets,
baseline, and Delta data remain under capacitance-specific fields.

Session CSV/XLSX/MAT/H5 export records measurement mode and quantity rather than
placing VOLT or RES values into pF-named fields. Legacy CAP session and replay
files remain supported where their schema is unambiguous. Setup profiles default
missing legacy `acquisition.measurementMode` to `CAP`; configured external rail
fields are explicitly named `voltageRail.measuredAvddV` and
`voltageRail.measuredAvssV` so they are not mistaken for live telemetry.

## Desktop behavior

The single workspace retains the 8x8 heatmap, selection, trends, resizable
splitters, Write / Command panel, Raw Log, and Status. Presentation changes by
quantity:

- CAP shows pF or Delta C/C0 %, offsets, and baseline controls.
- VOLT shows engineering voltage units and signed values.
- RES shows engineering resistance units.
- VOLT/RES tooltips show raw fixed values, physical values, PGA/bypass,
  validity, freshness, error reason, frame sequence, generation, request ID,
  source transport, and available rail/reference/retry diagnostics.

Invalid and inactive cells are `null`, not zero. Invalid or stale values do not
enter auto colour ranges, baseline calculations, statistics, or valid trend
series. Colour domains reset when the quantity changes, and trend history is
filtered by mode so pF, V, and ohm values never share an axis.

`ADS,chip=unknown,valid=0` is shown as **ADS identity unconfirmed**, never as a
guessed ADS1262. Battery telemetry is shown with validity, freshness, age,
reason, restore, retry, unstable, timeout, spread, and run diagnostics where
present; the host does not invent a battery state-of-charge percentage.

## Validation

Run the complete software gates from the repository root:

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

Formal GUI acceptance launches the locally built renderer in Electron; the
Electron main process starts the repository `.venv` Python sidecar and the
preload bridge supplies its dynamic loopback port. It does not use Chrome,
Vite, or a LAN renderer. Replay still traverses Transport -> Registry -> Parser
-> Store -> WebSocket -> React, and screenshots are saved under
`validation_artifacts/gui/`. Passing Vitest alone is not GUI acceptance.

Real hardware GUI acceptance uses the same full local application:

```powershell
cd desktop
npm.cmd run test:hardware
```

Hardware results are reported independently as Serial GUI, BLE GUI, and Wi-Fi
GUI PASS/FAIL/BLOCKED. A transport is PASS only after real hardware ran through
the GUI for the required scenarios. VOLT hardware validation is BLOCKED when a
current paired external DMM rail measurement is unavailable; no nominal or
monitor value may be fabricated. See [validation](docs/validation.md).

### Current firmware transport limitation

In the authoritative firmware implementation, accepted control responses reach
the initiating transport, but frame-boundary applied events such as `MAPP`, rail
and rows `RAPP`, `ADSCHK`/`ADSCHKSTAT`, and `BAPP` are currently printed on the
Serial event path and are not published to BLE `FF30` or Wi-Fi LOG. Therefore a
strict BLE-only or Wi-Fi-only host cannot prove a transaction applied. It must
remain pending and eventually show a timeout; it must not infer success from a
new data frame. BLE/Wi-Fi transaction HIL requires a Serial observation sidecar
or a firmware change that broadcasts applied events. This is a firmware
capability blocker, not a Replay PASS or host PASS.

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

The retained packaged-app smoke launches that executable directly with
Playwright Electron, rejects non-`file:` renderers, and verifies the packaged
backend health and preload bridge:

```powershell
cd desktop
npm.cmd run test:packaged
```

The root `icon.png` remains the source for generated desktop icons:

```powershell
.\.venv\Scripts\python.exe scripts\generate_icons.py
```
