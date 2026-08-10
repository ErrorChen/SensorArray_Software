# Validation

Validation results are reported in separate evidence classes. A lower layer
cannot be promoted to a higher one:

1. protocol and Python backend tests;
2. frontend typecheck, lint, unit tests, and production build;
3. GUI Replay E2E through the real backend and WebSocket;
4. Electron/preload/backend-sidecar smoke;
5. Serial hardware GUI acceptance;
6. BLE hardware GUI acceptance;
7. optional Wi-Fi hardware GUI smoke;
8. packaged `win-unpacked` smoke.

Replay PASS is not Serial, BLE, or Wi-Fi PASS. Vitest/build PASS is not GUI
acceptance. A transport is hardware PASS only when a real device has been
operated through the GUI and the required evidence was observed.

## Software gates

Run from the repository root with the repository virtual environment:

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

The protocol tests must cover current firmware-derived bytes for CAP, VOLT,
RES, signed negative voltage, all PGA values and bypass, known/unknown `Xhh`,
independent masks, good/bad CRC (including `P` in the V/R scope), rows
1/2/4/8, duplicate/missing chunks, recovery, MACK/MAPP correlation, generation
filtering, RAILCFG, ADSCHK, battery telemetry, generic store/snapshot state,
session export/import, and Replay end to end.

When a sibling firmware checkout is present, run the optional cross-repository
golden compatibility check. Normal host tests must still work offline using
fixtures committed under the host test tree.

## GUI Replay acceptance

The formal acceptance command launches Electron with the locally built
`dist/index.html`; Electron starts the repository `.venv` Python sidecar and
the preload bridge supplies its dynamically selected loopback port. It does
not use Chrome, a Vite URL, or a LAN-hosted renderer. Replay bytes then
traverse:

```text
Replay Transport
 -> ProtocolRegistry
 -> frame parser
 -> domain event
 -> MatrixStore/history
 -> backend snapshot
 -> WebSocket
 -> React/ECharts
```

The acceptance run covers:

- G01 application boot, health, all workspace panels, and no fatal console error;
- G02 current 8x8 CAP regression, pF, trends, offsets, baseline, and Delta;
- G03 visible CAP/VOLT/RES measurement selector and applied state;
- G04 MACK-only pending VOLT without premature commit;
- G05 matching MAPP plus signed VOLT frame and CAP-control isolation;
- G06 PGA x1/x2/x4/x8/x16/x32 and bypass tooltips;
- G07 `X03` invalid/error display without a zero value;
- G08 old-generation rejection and matching-generation recovery;
- G09 bad CRC rejection, Status diagnostic, and next-frame recovery;
- G10 pending/applied RES, ohm presentation, raw mOhm, and PGA;
- G11 active rows 1, 2, 4, and 8 without phantom zero cells;
- G12 RES -> CAP return with CAP features intact;
- G13 battery and ADS status, especially identity unconfirmed;
- G14 malformed-frame and unknown-log recovery without a crash;
- G15 main/bottom splitter and window resize behavior;
- G16 actual Electron preload bridge and backend sidecar smoke.

Screenshots are retained under `validation_artifacts/gui/` as
`G01_boot.png`, `G02_cap.png`, `G04_volt_pending.png`, `G05_volt.png`,
`G06_volt_pga.png`, `G07_invalid.png`, `G10_res.png`, `G12_cap_return.png`,
and `G16_electron.png`. Test scripts/configuration stay in the repository;
generated screenshots/traces need not enter the production package. On E2E
failure, retain console/network evidence and `trace.zip` when available.

## Electron smoke

The G01-G16 suite above is the formal Electron smoke and uses the local built
renderer plus the Python sidecar:

```powershell
cd desktop
npm.cmd run test:e2e
```

`npm.cmd run desktop` remains available for interactive development, where it
uses Vite hot reload; it is not the formal GUI acceptance path.

Acceptance requires a non-blank workspace, a working `window.sensorarrayDesktop`
preload bridge, healthy dynamically selected backend port, working WebSocket,
and no preload ESM/CJS or fatal renderer/backend error.

## Hardware readiness

Before sending mode commands after boot, wait for a complete, CRC-valid, fully
fresh CAP frame. An early `ADSBOOT` line is not readiness. Close other serial
monitors and BLE clients so they do not own the device.

Run hardware acceptance from the built local Electron application, which
starts its own Python sidecar:

```powershell
cd desktop
npm.cmd run test:hardware
```

The hardware script rejects any renderer URL that is not `file:` and records
the preload-provided backend URL in `validation_artifacts/hardware/`.

VOLT hardware acceptance requires a paired, current external DMM measurement of
AVDD -> GND and AVSS -> GND under the same power, wiring, and load. Convert the
readings to positive/negative integer uV, apply `RAILCFG` in CAP/RES, and wait
for matching `RACK` and rail `RAPP` before `MODE=VOLT`. Nominal rails, battery
telemetry, or the onboard ADS supply-monitor value cannot be substituted. If
the DMM pair is unavailable, report exactly:

```text
BLOCKED: measured AVDD/AVSS required
```

That blocker applies to VOLT HIL only; perform the remaining safe CAP/RES
checks where hardware is available.

## Serial GUI acceptance

Use the GUI Serial setup at 115200 baud:

1. Refresh ports, select the real SensorArray port, and connect.
2. Confirm sustained complete CAP frames; run CAP for at least 30 seconds.
3. Exercise ROWS 1, 2, 4, and 8, waiting for `RCMD`, matching `RAPP`, and a
   complete new-geometry frame each time.
4. CAP -> VOLT only with the measured rail workflow; run at least 30 seconds.
5. VOLT -> RES, run at least 30 seconds; RES -> CAP, run at least 30 seconds.
6. Inspect heatmap, selected-cell trends/tooltips, units, freshness/errors,
   Raw Log, Status, `MODE?`/`STATE?`, and command TX.
7. Verify MACK remains pending until matching MAPP, old generation does not
   overwrite the display, and matrix refresh remains continuous.
8. Disconnect, reconnect through the GUI, and recheck streaming.

PASS requires no backend/frontend exception, parser runaway, unexplained CRC
failure, stale-generation overwrite, unit mismatch, or mode/firmware mismatch.
If VOLT is blocked, report CAP/RES evidence independently rather than declaring
the whole Serial link untested.

## BLE GUI acceptance

Use the GUI Bluetooth LE flow: Scan -> verified SensorArray -> Connect. Verify
service `00FF`, CTRL RX/TX `FF10`/`FF11`, DATA `FF20`, LOG `FF30`, notify
subscriptions, and fragmentation/reassembly diagnostics.

Run CAP for at least 30 seconds, exercise selection/tooltips/commands/ROWS, then
attempt CAP -> VOLT (only with measured rails), VOLT/RES/CAP transitions, each
available mode for at least 30 seconds, Disconnect/Reconnect, and another live
run. Watch `BLE_RX50`, `BLE_FRAG50`, `PROTO50`, DATA/LOG/CTRL counts, and reset
breadcrumbs.

Current firmware has a known capability limit: applied events including `MAPP`,
rail/rows `RAPP`, `ADSCHK`/`ADSCHKSTAT`, and `BAPP` are emitted on Serial and not
broadcast on BLE `FF30`. A strict BLE-only GUI must therefore remain pending and
time out instead of inferring application from DATA. Full BLE transaction PASS
requires a Serial observation sidecar or firmware support for publishing these
events. Report this as a firmware capability BLOCKED/FAIL, not as host PASS.

For any BLE failure retain a minimal complete record: connection state, service
and characteristics, notify map, DATA/LOG/CTRL counts, fragment/parser stats,
first/last payload prefix, MatrixStore update count, backend exception, and any
firmware reset breadcrumb. “BLE doesn't work” is insufficient evidence.

## Wi-Fi GUI smoke

When the network environment permits, use GUI discovery/connect and verify DATA
3333, LOG 3334, CTRL 3335, command TX, matrix refresh, and disconnect. The same
applied-event limitation currently affects strict Wi-Fi-only transactions.

## Packaging smoke

From the root, build the sidecar and Windows target:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\package_windows.ps1
```

At minimum launch `desktop/release/win-unpacked/SensorArray.exe` and repeat the
Electron boot/preload/backend health checks. If a full NSIS build is not run,
report it separately rather than implying installer acceptance.

After `win-unpacked` exists, run the retained delivery-state smoke:

```powershell
cd desktop
npm.cmd run test:packaged
```

This command starts `win-unpacked\SensorArray.exe` itself. It requires an
`app.asar` local `file:` renderer, the packaged preload bridge, and a healthy
PyInstaller backend on a dynamically selected loopback port; it does not start
Chrome or a Vite server.

After validation, close Electron and its Python sidecar, plus any development
Vite/Uvicorn processes, serial readers, and BLE connections. Preserve required
screenshots and minimal failure evidence; do not confuse generated evidence
with production application assets.
