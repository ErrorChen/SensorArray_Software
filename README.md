# SensorArray Desktop Host

Desktop host software for the SensorArray b41 8x8 matrix.

Firmware protocol baseline:

```text
b41c5256fbb5b23a0f0d98ed651db2f6ced3a0d6
```

The default user interface is now an Electron desktop application with a React
frontend and a Python FastAPI backend. It does not open an external browser.

## Architecture

```text
Electron main process
-> starts Python backend sidecar
-> waits for GET /health
-> opens desktop BrowserWindow
-> terminates the backend child process on application exit

React + TypeScript frontend
-> Setup panel
-> ECharts 8x8 heatmap
-> four independent ECharts trend charts
-> raw/event log panel
-> REST commands and WebSocket snapshots

FastAPI backend
-> Serial / BLE / Wi-Fi UDP / Replay transports
-> b41 C/D/K parser
-> fixed-point capacitance conversion
-> matrix and history stores
-> baseline and delta C/C0 %
-> REST API and /ws realtime stream
```

The reusable protocol, transport, domain, and store code is kept under
`src/sensorarray_app`. The desktop backend service is under
`src/sensorarray_backend`. The previous browser UI, previous event handler
files, and previous standalone viewer have been removed from the default
application path.

## Install

Python:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Desktop:

```powershell
cd desktop
npm install
```

## Run

Backend only:

```powershell
.\.venv\Scripts\python.exe -m sensorarray_backend --host 127.0.0.1 --port 8765
```

Frontend dev server:

```powershell
cd desktop
npm run dev
```

Desktop app:

```powershell
cd desktop
npm run desktop
```

`npm run desktop` starts Vite, launches Electron, and lets Electron start the
Python backend sidecar.

## API

Required endpoints:

```text
GET  /health
GET  /api/status
POST /api/transport/mode

GET  /api/transport/serial/ports
POST /api/transport/serial/connect

GET  /api/transport/ble/scan
POST /api/transport/ble/connect

GET  /api/transport/wifi/discover
POST /api/transport/wifi/connect

POST /api/transport/disconnect

POST /api/replay/open
POST /api/replay/start
POST /api/replay/stop
POST /api/replay/seek

POST /api/rows
POST /api/settings/display
POST /api/settings/baseline
POST /api/selection

WS   /ws
```

Realtime data goes through WebSocket, not REST polling.

## Serial

The application scans ports with `serial.tools.list_ports.comports()` and fills
a dropdown. COM12 is only a hardware validation port; it is not a default port
or hidden fallback. Baud defaults to 115200.

## Bluetooth LE

BLE uses `bleak`. Selecting Bluetooth LE triggers a scan. Results are structured
objects with name, address, RSSI, service UUID hints, verification flags, and a
match reason. Verified SensorArray candidates are sorted before weak or unnamed
advanced entries. Runtime data is received through notify/indicate
characteristics. If the expected UUIDs are not present, the backend logs the
available notify characteristics instead of failing silently.

## Wi-Fi UDP

Selecting Wi-Fi UDP triggers discovery. Discovered hosts populate a dropdown.
The fallback host field remains available, for example `192.168.4.1`, but it is
not the primary path. DATA, LOG, and CTRL remain separate UDP channels.

## Replay

Replay files are selected with Electron's native file dialog. Replay data uses
the same parser, matrix store, snapshot builder, WebSocket path, heatmap, and
trend charts as live transports. Replay is software validation only and does not
replace COM12 or BLE hardware validation.

## ROWS

ROWS supports 1 through 8. Live transports send `ROWS=n` to the device through
the existing command service. `RCMD` means accepted; only `RAPP` means applied.
Replay or disconnected mode is display-only and is reported as such.

## Capacitance

b41 C/D/K DATA values are fixed-point capacitance integers:

```text
rawPf = rawFixed / 1_000_000.0
correctedPf = rawPf - 33.0
```

`-1000000` is the invalid sentinel. Invalid and inactive cells become null/NaN,
not zero. The heatmap tooltip shows cell label, corrected pF, raw pF, rawFixed,
frame seq, and validity.

Display modes:

- Absolute C: corrected pF.
- Delta C/C0 %: `(currentPf - baselinePf) / baselinePf * 100`.

`Set baseline` captures a backend baseline session. Pause display freezes visual
updates while the backend continues receiving and logging frames. Freeze colour
keeps the current heatmap colour range.

## Selection And Trends

The matrix is fixed to rows `S1..S8` and columns `D1..D8`. Clicking any cell in
D1-D4 selects that row's primary FDC group. Clicking any cell in D5-D8 selects
that row's secondary FDC group. The trend area renders four independent charts,
one per selected cell.

## Test

```powershell
.\.venv\Scripts\python.exe -m compileall src tests
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pytest -q

cd desktop
npm run typecheck
npm run lint
npm run test
npm run build
```

## Hardware Validation

Software checks and replay are not enough for completion. Real hardware
validation must use:

- Serial COM12, selected from the scanned dropdown, for at least 120 seconds.
- Bluetooth LE auto scan, selected device, notify data path, for at least 120 seconds.

Do not save screenshots or long raw sensor dumps. After validation, close
Electron, Vite, Uvicorn, backend child processes, BLE scanner/readers, and serial
reader threads. Temporary validation logs should be deleted unless they are the
minimal failure summary needed to diagnose a failed run.

## Troubleshooting

- Backend port occupied: Electron tries available ports starting at 8765.
- Serial open failure: close other serial clients and rescan the dropdown.
- COM12 missing: report that hardware validation could not run; do not default
  to another port and call it COM12 validation.
- BLE no device found: rescan and enable advanced entries only if needed.
- BLE GATT mismatch: inspect raw log entries for service and notify details.
- Wi-Fi timeout: verify the SoftAP connection and try the fallback host.
- Replay parse failure: confirm the file is a b41 C/D/K, BLE/Wi-Fi envelope, or
  supported legacy capture.
