# SensorArray Desktop Host

Desktop host software for the SensorArray b41 8x8 matrix. The default user
interface is an Electron desktop application with a React frontend and a Python
FastAPI backend sidecar.

Firmware protocol baseline:

```text
b41c5256fbb5b23a0f0d98ed651db2f6ced3a0d6
```

## Environment

Python backend work should use the repository `.venv`. Electron/React uses the
system Node.js and npm installation. You do not need to exit `.venv` before
running npm commands.

```powershell
.\.venv\Scripts\python.exe --version
node --version
npm.cmd --version
```

On Windows, `npm` may be blocked by PowerShell execution policy. Use `npm.cmd`
from the same terminal, or refresh VS Code/PATH. Do not bake local absolute
paths into source files.

## Install And Run

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

cd desktop
npm install
npm run desktop
```

`npm run desktop` starts Vite, launches Electron, and lets Electron start the
Python backend sidecar. Backend-only development is still available:

```powershell
.\.venv\Scripts\python.exe -m sensorarray_backend --host 127.0.0.1 --port 8765
```

## Icons

The only source icon is the repository root `icon.png`. All other icon assets
are generated from it:

```powershell
.\.venv\Scripts\python.exe scripts\generate_icons.py
```

The script center-crops `icon.png`, generates PNG sizes under
`desktop/assets/icons/`, creates `sensorarray-icon.ico`, and syncs
`desktop/public/favicon.ico`, `icon-192.png`, and `icon-512.png`. Electron uses
`desktop/assets/icons/sensorarray-icon.ico` in development and checks packaged
resource locations when packaged. Windows may cache old taskbar/window icons;
restart Electron or clear the Windows icon cache if the generated icon does not
appear immediately.

## Architecture

```text
Electron main process
-> starts Python backend sidecar
-> waits for GET /health
-> opens SensorArray BrowserWindow with generated icon
-> terminates the backend child process on application exit

React + TypeScript frontend
-> resizable heatmap/setup/trend workspace
-> Write / Command panel
-> Raw Log / Event Log panel
-> REST commands and WebSocket snapshots

FastAPI backend
-> Serial / BLE / Wi-Fi UDP / Replay transports
-> content-routed b41 C/D/K parser
-> matrix and history stores
-> baseline and delta C/C0 %
-> REST API and /ws realtime stream
```

Reusable protocol, transport, domain, and store code is under
`src/sensorarray_app`. The desktop backend service is under
`src/sensorarray_backend`.

## API

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

POST /api/transport/write
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

`POST /api/transport/write` writes through the active transport abstraction:

```json
{
  "text": "COMMAND",
  "lineEnding": "lf",
  "encoding": "utf-8",
  "mode": "text"
}
```

Successful responses include `transport` and `bytesWritten`. Unsupported modes,
disconnected state, missing BLE ctrl characteristic, or write failures return
`ok: false` with a clear error. Replay write is intentionally unsupported.

## Desktop UI

Connect and Disconnect are a single primary action in the active setup mode.
The button state comes from `snapshot.connection.mode` and
`snapshot.connection.state`, so the top status bar and setup panel stay
consistent. Switching tabs does not disconnect an active transport.

The main workspace is resizable:

- Left pane: 8x8 heatmap.
- Right pane: setup and 2x2 trend charts.
- Default split: 75% / 25%.
- Stored key: `sensorarray.layout.mainSplitRatio`.

The bottom area is also resizable:

- Left pane: Write / Command.
- Right pane: Raw Log / Event Log.
- Default split: 50% / 50%.
- Stored key: `sensorarray.layout.bottomSplitRatio`.

## Heatmap And Trends

Invalid and inactive cells are sent to the heatmap as `null`, not `0`, and do
not participate in colour range calculation. The heatmap series explicitly maps
`x: 0`, `y: 1`, and `value: 2`; `visualMap.dimension` is also `2`.

Colour modes:

- Auto colour: every frame derives the range from valid finite cells only.
- Freeze colour: keeps the last range and new frames do not overwrite it.

Absolute pF ranges include padding. Delta percent ranges are symmetric around
zero and at least +/-0.5%. Tooltips show cell label, corrected pF, raw pF,
rawFixed, validity, frame sequence, and source transport. Heatmap and trend
charts use `ResizeObserver` so they resize with their panes.

Clicking D1-D4 selects that row's primary FDC group. Clicking D5-D8 selects that
row's secondary FDC group. Trend history is limited to the latest 600 points,
and invalid cells do not enter valid trend series.

## Write / Command

The Write / Command panel sends text commands through
`POST /api/transport/write`; it does not access Serial, BLE, or Wi-Fi directly.

Controls:

- Active transport label.
- Multiline command input.
- Append LF, Append CRLF, or No line ending.
- Ctrl+Enter sends; Enter inserts a newline.
- History keeps the latest 20 commands in `sensorarray.command.history`.
- Send is disabled while disconnected, connecting/disconnecting, pending, or
  when the input is empty.

Each write produces a short backend log:

```text
CMD_TX,mode=serial,bytes=...,ending=lf
CMD_TX_FAIL,mode=ble,error=...
```

Long command/error text is truncated in logs and UI records.

## Bluetooth LE

BLE uses `bleak`. BLE scan is disabled while BLE is connecting, connected, or
streaming. The backend also rejects `/api/transport/ble/scan` in that state with:

```text
BLE scan is disabled while connected; disconnect first.
```

BLE notify channels are normalized before routing:

- `data`, `d`, `cap`, `caps`, `c`, `capacitance` -> `data`
- `log`, `logs`, `l` -> `log`
- `ctrl`, `control`, `cmd`, `command` -> `ctrl`

The protocol registry routes by payload content, not channel alone. If `C/D/K`
frames arrive through a log or `L` characteristic, they still enter
`CapAsciiParser` and update MatrixStore. `SF50`, `TR50`, `AB50`, `OT50`,
`BL50`, and `I2C50` remain log events and do not pollute matrix data.

BLE diagnostics are aggregated rather than dumped per notify:

- `BLE_RX50`: notify counts/bytes/reassembled/failures and last prefix.
- `BLE_FRAG50`: fragment rx/reassembled/duplicate/missing/timeout/crc/length.
- `PROTO50`: parser cap/log/reject/frame counters.

Parser, CRC, fragment, or length errors are counted and logged but do not stop
the BLE transport unless the underlying connection actually closes.

## Transport Notes

Serial ports are discovered with `serial.tools.list_ports.comports()`. COM12 is
only a hardware validation example and is not hardcoded.

Wi-Fi UDP keeps DATA, LOG, and CTRL channels separate. Command write uses the
CTRL UDP port when Wi-Fi is active.

Replay uses the same parser, matrix store, snapshot builder, WebSocket path,
heatmap, and trend charts as live transports. Replay is software validation
only and does not replace serial or BLE hardware validation.

## ROWS

ROWS supports 1 through 8. Live transports send `ROWS=n` through the existing
command service. `RCMD` means accepted; only `RAPP` means applied. Replay or
disconnected mode is display-only and is reported as such.

## Validation

```powershell
.\.venv\Scripts\python.exe --version
node --version
npm.cmd --version

.\.venv\Scripts\python.exe scripts\generate_icons.py

.\.venv\Scripts\python.exe -m compileall src tests scripts
.\.venv\Scripts\python.exe -m pytest -q

cd desktop
npm.cmd install
npm.cmd run typecheck
npm.cmd run lint
npm.cmd run test
npm.cmd run build
```

## Hardware Validation

Serial:

1. Select Serial.
2. Select the scanned hardware port, for example COM12 if present.
3. Baud 115200.
4. Connect and run for at least 120 seconds.
5. Verify heatmap data, trend data after cell selection, Auto/Freeze colour,
   both splitters, command TX log, and Disconnect returning to Connect.

BLE:

1. Select Bluetooth LE.
2. Scan, select the SensorArray device, then Connect.
3. Run for at least 120 seconds.
4. Verify BLE connected/streaming, Scan disabled, no repeated scan found logs,
   `BLE_RX50` / `BLE_FRAG50` / `PROTO50`, heatmap/trend data, clear command
   write success or missing-ctrl error, and graceful Disconnect.

If BLE still has no matrix data, capture the minimal failure summary: notify
map, data/log/ctrl counts, first/last payload prefix, fragment stats, parser
stats, whether `C/D/K` was detected, whether `CapAsciiParser` ran, whether
MatrixStore updated, and state transitions.

After validation, close Electron, Vite, Uvicorn/backend sidecars, BLE readers,
and serial readers. Do not keep screenshots or long raw dumps unless they are
the minimal failure evidence for a failed run.
