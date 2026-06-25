# Validation

Validation for the desktop refactor has two layers: software checks and real
hardware checks. Replay is useful for software validation but does not replace
COM12 or BLE validation.

Software checks are expected to run from repository `.venv`:

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

Hardware validation order:

1. Start Electron with `cd desktop; npm run desktop`.
2. Select Serial mode, let the app scan ports, choose COM12 from the dropdown,
   connect at 115200 baud, and run for at least 120 seconds.
3. Disconnect and reconnect COM12 once.
4. Select Bluetooth LE mode, let the app auto scan, choose the SensorArray-like
   device, connect through notify, and run for at least 120 seconds.
5. Disconnect, rescan, and reconnect BLE once.

Do not claim pass for a transport unless it ran against real hardware for the
required duration. After validation, close Electron, backend, Vite, Uvicorn,
BLE, and serial readers, and delete temporary validation logs.
