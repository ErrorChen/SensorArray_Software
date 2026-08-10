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

The setup panel separates transport selection from measurement quantity:

- Transport: Serial, Bluetooth LE, Wi-Fi UDP, or Replay.
- Measurement Mode: CAP, VOLT, or RES, with separate applied/pending/error state.
- Serial: scan ports, choose dropdown, connect.
- Bluetooth LE: scan, choose a verified device, connect.
- Wi-Fi UDP: discover, choose a device or fallback host, connect.
- Replay: choose a file with the native dialog, start or stop playback.

The measurement selector does not change `connection.transportMode`. `MACK`
only creates pending state; a matching `MAPP` commits the applied quantity.
VOLT Setup also collects paired external measured AVDD/AVSS values and applies
the rail transaction before requesting VOLT. CAP offset/baseline/Delta controls
remain visible only for capacitance behavior.

The frontend uses WebSocket snapshots for realtime data. REST is used only for
commands and discovery actions.
