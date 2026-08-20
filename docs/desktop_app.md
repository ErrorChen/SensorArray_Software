# Desktop App

The Electron/React desktop app is in `desktop/`:

```powershell
cd desktop
npm.cmd install
npm.cmd run desktop
```

Electron starts the Python backend sidecar, waits for `/health`, opens the
React window through the preload bridge, and stops its child process on exit.
The renderer receives typed REST responses and WebSocket snapshots; it never
parses firmware text directly.

## Workspace

```text
status bar
Nx8 heatmap | setup panel
             | four trend charts
raw/event log
```

The setup panel separates transport, active geometry, and measurement profile:

- Transport: Serial, Bluetooth LE, Wi-Fi UDP, or Replay.
- ROWS: every integer 1 through 8.
- Set all rows: CAP, VOLT, or RES.
- Row measurement modes: independent S1 through S8 selectors and one atomic
  Apply action.
- Serial/BLE/Wi-Fi discovery and connect controls.
- Replay file, speed, start, stop, and seek controls.

Transport selection never changes the measurement mode. Measurement actions
never change `connection.transportMode`.

## Global set-all quick action

The existing CAP/VOLT/RES buttons remain as a backwards-compatible quick
action and are labelled as setting all rows. Each sends one legacy command:

```text
CAP  -> MODE=CAP  -> all eight saved row modes become CAP on matching MAPP
VOLT -> MODE=VOLT -> all eight saved row modes become VOLT on matching MAPP
RES  -> MODE=RES  -> all eight saved row modes become RES on matching MAPP
```

`MACK` changes the UI to accepted/pending but does not change the applied mode
or profile. Only matching `MAPP` commits the generation and boundary sequence.
Timeout/error remains visible, and seeing target-mode data cannot complete the
request.

## Atomic row-mode editor

The **Row measurement modes** section always displays S1 through S8. Each row
has a CAP/VOLT/RES selector. Editing changes local draft state only. Pressing
**Apply row modes** sends exactly one eight-entry API request, which the backend
encodes as one `ROWMODES=<8 characters>` command.

The panel shows applied profile, pending profile, transaction state, and error
separately. `RMACK` changes state to accepted; applied profile stays unchanged
until matching `RMAPP`. `RMERR`, timeout, wrong request ID, or wrong profile
does not fabricate application. The draft can remain available for correction
and retry.

Firmware `8045e9e9` completes homogeneous and heterogeneous `ROWMODES`
requests with `RMAPP` or `RMERR`. The UI still keeps the old applied profile
until the matching terminal and never infers success from a C/V/R data frame.

Rows greater than current `ROWS` remain visible because the firmware profile
always stores eight entries. They are dimmed and labelled **Inactive with
current ROWS setting**. Increasing ROWS reactivates their saved configuration.

## Active heatmap geometry

The heatmap uses `activeRows=clamp(snapshot.frame.rows,1,8)` everywhere:

- title (`1x8`, `5x8`, or `8x8`);
- y-axis labels and row-mode suffixes;
- data and invalid-cell layers;
- selection/hover overlays and pointer hit testing;
- keyboard navigation;
- CSS grid rows.

Reducing ROWS clears or corrects a selection that is outside the active
geometry. `ROWS=1` therefore displays S1 with D1-D8 and no S2-S8 ghost rows.

## Homogeneous and mixed rendering

Homogeneous frames retain the familiar single CAP, VOLT, or RES heatmap. A
mixed frame remains one physical Nx8 matrix, but the renderer creates
separate mode-specific ECharts series and visual maps:

- CAP: pF, or Delta C/C0 % when selected;
- VOLT: V;
- RES: ohm.

Only rows of a mode enter that mode's series and colour range. Legends are
shown only for active modes. A typical label is `S1 · RES`, and a mixed tooltip
includes physical coordinate, mode, formatted value/unit, validity, freshness,
error, generation, and request ID.

CAP display, baseline, and offset controls remain available when at least one
active row is CAP. Baseline capture affects only current CAP cells. In Delta
C/C0 view, CAP rows change to percent while VOLT and RES remain absolute; their
scales never share a min/max calculation.

Trend queries use each selected cell's current row mode and mask other-mode
history as a discontinuity. Changing S1 from RES to CAP does not draw a line
from ohms to pF.

## Colour behavior

The backend publishes authoritative independent ranges for CAP absolute, CAP
delta, VOLT, and RES. Freeze state and last-good range are also per domain.
Only finite, valid, fresh, non-error cells inside active rows influence a range.

If a domain has one usable value or all usable values are equal, the fallback
uses domain semantics rather than forcing a particular colour for ROWS=1:

- positive CAP/RES uses zero as its lower bound and at least 105% of the value
  as its upper bound;
- signed VOLT and CAP delta use a symmetric zero-centred extent.

Thus a lone positive resistance cell is above the colour midpoint instead of
neutral white. Frontend fallback is retained only for legacy snapshots that do
not contain typed backend ranges.

## Read-only rail telemetry

The production UI has no editable AVDD-to-GND or AVSS-to-GND inputs. Normal
VOLT and row-profile actions do not configure rails. Firmware owns its
automatic internal ADS rail monitor, and the panel shows a read-only result:

```text
ADS analogue rail span
AVDD - AVSS: 5.126 V
fresh
source: internal monitor
```

A stale value keeps the span and displays its age. Missing/invalid telemetry is
shown as **Rail unavailable** with the reported reason. The legacy rail API and
raw command remain available only for debug/old replay compatibility and are
not exposed as a normal setup workflow.

## Battery status

Battery status comes from backend typed state, not React local storage:

- never measured: `Battery —`;
- fresh: `Battery 4.092 V (fresh)`;
- stale: `Battery 4.092 V (last known · stale)`;
- failed attempt: `Battery 4.092 V (last known · adc_timeout)`.

Firmware `8045e9e9` authoritative `lastGoodMv/lastGoodValid/lastGoodFresh/
lastGoodAgeMs/lastGoodFrame` fields are preferred. With older firmware,
the backend retains the last valid value for the same device session. A later
invalid attempt changes state/reason without erasing the number; a later fresh
sample replaces it. A different device identity clears the cache.

## Setup-profile migration

The setup profile stores:

```json
{
  "acquisition": {
    "rows": 5,
    "measurementMode": "CAP",
    "rowModes": ["RES", "VOLT", "VOLT", "CAP", "CAP", "VOLT", "VOLT", "RES"]
  }
}
```

`rows` accepts all integers 1 through 8. `rowModes` must contain exactly eight
CAP/VOLT/RES values. An old profile without it is migrated by repeating the old
`measurementMode`, or CAP if that field is absent. Old `voltageRail` fields are
still readable/round-trippable but are neither displayed nor automatically
sent.

## Replay and hardware evidence

Replay uses the same registry/parser/domain/store/snapshot/UI path and covers
ROWS=1/3/5/8, homogeneous and mixed layouts, transaction state, rail readout,
battery last-known state, and independent colour domains. Formal E2E launches
real Electron with the built renderer and Python sidecar.

Replay still does not prove that firmware emitted M/MR/K bytes or delivered
`MAPP`/`RMAPP` on BLE `FF30`. Exact source compatibility targets firmware
`8045e9e9ec9599533c52c15dfcb6002f79fd15f1`; real BLE observations are
reported separately from Replay PASS.
