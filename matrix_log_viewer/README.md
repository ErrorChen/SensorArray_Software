# Legacy Compatibility Package

The active SensorArray b41 host application now lives in `src/sensorarray_app` and is launched with:

```powershell
.\.venv\Scripts\python.exe -m sensorarray_app
```

This directory is kept for legacy parser/store tests and historical imports. `run_viewer.py` and `run_gui.py` are thin wrappers into the new entry point.

Current b41 capacitance data is C/D/K ASCII with dynamic `rows * 8` cells. Legacy `SAC1` / `FAST_BINARY` remains voltage-only compatibility and must not be treated as capacitance.

See the root `README.md` and `docs/architecture.md` for the current protocol, transport, UI, and validation details.
