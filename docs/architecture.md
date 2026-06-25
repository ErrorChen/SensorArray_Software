# SensorArray Desktop Architecture

## Ownership

```text
desktop/electron      Electron process, backend child process, file dialog
desktop/src           React UI, ECharts rendering, REST commands, WebSocket state
src/sensorarray_backend
  api/                FastAPI routes and WebSocket endpoint
  core/               runtime wrapper, snapshot schema, history, selection, units
src/sensorarray_app
  domain/             typed events, baseline, battery, voltage, resistance
  protocol/           b41 C/D/K, CRC, BLE fragments, legacy protocol plugins
  transport/          Serial, BLE notify, Wi-Fi UDP, replay, discovery
  store/              matrix, history, raw logs, telemetry, statistics
  services/           command and discovery services
```

## Data Flow

```text
Device or replay
-> transport envelope
-> ProtocolRegistry
-> typed domain event
-> MatrixStore / RawLogStore / TelemetryStore
-> backend snapshot
-> WebSocket /ws
-> React state
-> ECharts heatmap and four independent trend charts
```

The frontend never parses device protocols and never owns transport state.

## Runtime Contracts

- One active transport at a time.
- Session generation changes on connect, disconnect, replay restart, and transport switch.
- Late packets from older sessions are ignored by state handling.
- Serial requires an explicit scanned port.
- BLE data is notify/indicate first; read characteristic polling is not the main path.
- Wi-Fi DATA, LOG, and CTRL remain separate UDP channels.
- Replay uses the same parser/store/WebSocket path as hardware.

## Snapshot Contract

Every WebSocket snapshot contains:

```text
connection
frame
matrix.correctedPf
matrix.rawPf
matrix.rawFixed
matrix.validMask
selection.title
display.displayMode
display.measurementDomain
display.showCellText
display.pauseDisplay
display.freezeColor
display.circuitOffsetPf
```

The backend explicitly serializes `selection.title`; it is not left as a Python
property that disappears during dataclass conversion. The frontend also has a
defensive title fallback for malformed external snapshots.

## Migration Map

| Previous item | New item | Action |
|---|---|---|
| `sensorarray_app.app.bootstrap` browser launcher | `sensorarray_backend.main` | removed from default path |
| browser layout/event handlers | `desktop/src/components/*` | replaced |
| standalone browser viewer | backend service + desktop UI | removed |
| root `main.py` | `sensorarray_backend.main` wrapper | retained as compatibility launcher |
| selection logic | `sensorarray_backend.core.selection` | centralized |
| fixed-point conversion | `sensorarray_backend.core.units` | centralized |
| matrix latest/history stores | `sensorarray_app.store.*` | retained and extended |
| serial/BLE/Wi-Fi/replay transports | `sensorarray_app.transport.*` | retained and cleaned |
