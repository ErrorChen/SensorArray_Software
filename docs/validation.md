# Validation Log

This file records commands and hardware validation status for the b41 host refactor.

Software checks are expected to run from repository `.venv`:

```powershell
.\.venv\Scripts\python.exe -m compileall src tests
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pytest -q
```

Hardware validation order:

1. Serial COM12 at 115200 baud.
2. BLE automatic discovery.
3. Wi-Fi automatic discovery.

Do not claim pass for a transport unless it ran against real hardware for the required duration. Replay/mock results are separate software validation only.
