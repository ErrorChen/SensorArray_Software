# Desktop App

The desktop app is in `desktop/`.

```powershell
cd desktop
npm install
npm run desktop
```

Electron starts the Python backend sidecar, waits for `/health`, opens the
React window, and stops its backend child process on exit.

The first screen is the measurement workspace:

```text
status bar
8x8 heatmap | setup panel
            | four trend charts
raw/event log
```

The setup panel is mode-first:

- Serial: scan ports, choose dropdown, connect.
- Bluetooth LE: auto scan on mode selection, choose dropdown, connect.
- Wi-Fi UDP: auto discover on mode selection, choose dropdown or fallback host, connect.
- Replay: choose file with native dialog, start or stop playback.

The frontend uses WebSocket snapshots for realtime data. REST is used only for
commands and discovery actions.

