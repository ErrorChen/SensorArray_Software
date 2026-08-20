# Host measurement protocol compatibility notes

This document describes how SensorArray Desktop Host consumes the measurement
and command protocol. It is a host compatibility contract, not an independent
firmware specification. Production firmware formatter/command code and golden
tests remain authoritative for real wire bytes.

## Contract status

The firmware validation baseline is
`8045e9e9ec9599533c52c15dfcb6002f79fd15f1`. The software revision under
validation must be recorded separately because it changes during repair.

That firmware revision provides the established homogeneous CAP/VOLT/RES
families, `MODE`, `ROWMODES`, heterogeneous `M/MR/K`, FF30 lifecycle events,
internal rail telemetry, and battery last-good fields. The production C
formatter in `main/output/sensorarrayTextProtocol.c` is the byte-level
authority. It emits short mixed identity keys, long `MR` modes
`CAP|VOLT|RES`, and comma-separated row data. Firmware `8045e9e9` completes
homogeneous and heterogeneous `ROWMODES` transactions with `RMAPP` or `RMERR`;
the Host retains strict request-ID correlation in both cases.

The firmware schema is consumed once by
`sensorarray_app.protocol.mixed_ascii.MixedMeasurementAsciiParser`. Replay
contains exact compatibility copies; React does not parse or redefine it.

## Streams, routing, and BLE

All transports expose the same logical channels:

```text
DATA -> measurement packets
LOG  -> telemetry and asynchronous applied events
CTRL -> command requests and immediate/accepted responses
```

Routing is by payload content. A complete measurement packet received on a LOG
channel still enters the appropriate frame assembler. BLE envelope length/CRC
checks and `BleFragmentReassembler` run before content routing.

The BLE service and characteristics remain:

| Item | UUID suffix | Role |
| --- | --- | --- |
| service | `00FF` | SensorArray service |
| CTRL RX | `FF10` | host command writes |
| CTRL TX | `FF11` | command response notify |
| DATA | `FF20` | measurement notify |
| LOG | `FF30` | log/asynchronous event notify |

`BleTransport` uses one BLE client and one subscription for each required
characteristic. The strict target transaction pipeline is:

```text
FF11 fragmented or single MACK/RMACK
FF30 fragmented MAPP/RMAPP
 -> BleTransport
 -> BleFragmentReassembler
 -> ProtocolRegistry
 -> TextLogProtocol
 -> CommandTransactionEvent
 -> CommandService
```

The frontend never parses raw `MAPP`/`RMAPP`, and the host does not add a second
BLE client or duplicate `FF30` subscription. A data frame on `FF20` is not a
substitute for an applied event on `FF30`.

## Common line and CRC rules

The protocol is ASCII and line oriented. Production frames use LF (`\n`). A
frame begins with one header and ends at its `K` trailer. The complete frame is
emitted to domain/store state only after all required records and identities
are present and its CRC is correct.

CRC uses the firmware reflected CRC-32. Its input is the exact encoded bytes of
the header and all body lines, including each LF and excluding the `K` line.
CRLF input is normalized to LF by the mixed host assembler before CRC. A bad
frame updates parser diagnostics but never MatrixStore; the next valid header
can recover.

`ROWS=n` accepts every integer `n` from 1 through 8. `cells` must equal
`rows * 8`; legacy homogeneous headers also carry `n=cells`, while canonical
mixed headers do not. Inactive high mask bits are rejected and inactive cells
remain null.

## Homogeneous CAP frame

CAP output retains its established form:

```text
C,seq=...,ts=...,rows=...,cells=...,gen=...,rid=...,rf=...,pf=...,sf=...,bad=.../.../...,fmt=pf6,n=...
D0,<fixed-pF values, up to 16>
D1,...
...
K,seq=...,gen=...,rid=...,crc=<8 hexadecimal digits>
```

`D` values are pF scaled by 1,000,000. The CAP invalid sentinel is detected
before conversion and is never treated as a physical negative pF value.
Circuit correction, user offset, baseline, and Delta C/C0 remain CAP-only host
operations. `rf`, `pf`, and `sf` retain row/primary/secondary freshness.

The `gen/rid` in a legacy C frame belong to the ROWS/configuration snapshot,
not the global mode transaction. After matching `MAPP` applies CAP, the host
uses its boundary `seq` plus the C frame type to reject pre-boundary data.

## Homogeneous VOLT and RES frames

VOLT and RES retain this form:

```text
V|R,seq=...,ts=...,rows=...,cells=...,gen=...,rid=...,mode=...,unit=...,scale=...,valid=...,fresh=...,error=...,ref=...,rail=...,age=...,...,fmt=...,n=...,bad=...
D0,<signed integer or Xhh>,...
...
P0,<two packed hexadecimal digits per cell>
...
K,seq=...,gen=...,rid=...,crc=<8 hexadecimal digits>
```

| Frame | Required header contract | Raw `D` value | Host physical value |
| --- | --- | --- | --- |
| VOLT | `V,mode=VOLT,unit=V,scale=-6` | signed uV | `raw * 10^-6 V` |
| RES | `R,mode=RES,unit=ohm,scale=-3` | mOhm | `raw * 10^-3 ohm` |

`valid`, `fresh`, and `error` are independent masks. Only finite cells that are
valid, fresh, and not in error can enter live colour, baseline, statistics, or
trend calculations. `Xhh` is an invalid cell with firmware error code `0xHH`;
the token, code, and reason are retained. Unknown codes remain unknown rather
than becoming zero.

Each `P<n>` body is packed, not comma separated. Each cell has two hexadecimal
digits: `01/02/04/08/10/20` for PGA x1/x2/x4/x8/x16/x32 and `00` for verified
bypass. VOLT/RES CRC scope includes all `P` records.

## Global measurement-mode transaction

The legacy command remains the quick action for setting all eight rows:

```text
MODE=CAP|VOLT|RES
MACK,id=<requestId>,old=<mode>,new=<mode>,state=accepted
MAPP,id=<requestId>,gen=<generation>,old=<mode>,new=<mode>,seq=<frameSeq>,state=applied,transitionUs=<us>
```

The host state machine is strict:

1. Send one `MODE` request.
2. Match `MACK` by request ID and requested mode; expose accepted/pending state.
3. Keep `appliedMode` and the applied all-row profile unchanged.
4. Commit only a matching `MAPP`; retain its generation, request ID, and frame
   boundary sequence.
5. Reject old/mismatched frames after that boundary.
6. On timeout, `ERR`, or `MERR`, expose the error without fabricating applied
   state.

`CommandService.observe_mode_frame()` returns false while a mode transaction is
pending. Seeing a RES/VOLT/CAP frame cannot complete the transaction. A
same-mode request can still create a new generation and follows the same rules.

## Atomic row-mode profile transaction

The profile always contains eight characters, independently of active `ROWS`:

```text
ROWMODES?
ROWMODES=RVVCCVVR
RMACK,id=62,old=CCCCCCCC,new=RVVCCVVR,state=accepted
RMAPP,id=62,gen=11,seq=201,profile=RVVCCVVR,state=applied
RMERR,id=63,gen=12,seq=202,profile=CRVCRVCR,err=0x108,state=rejected,route=SAFE
```

Characters map as `C -> CAP`, `V -> VOLT`, and `R -> RES`. The frontend edits
an eight-entry draft but **Apply row modes** sends exactly one command. There
are no per-row command loops.

Row-profile state is independent of the global mode state:

```text
appliedRowModes
pendingRowModes
rowModeRequestId
rowModeGeneration
rowModeFrameSeq
rowModeTransitionState
rowModeError
```

`RMACK` establishes accepted/pending state only. Matching requires the same
request ID and profile. Only matching `RMAPP` commits the profile and boundary.
A wrong-ID/profile `RMAPP` is rejected. `RMERR` or timeout leaves the last
applied profile intact; a UI draft may remain for retry.

A complete mixed frame can synchronize the profile on first attachment only
when no row-profile request is pending. It cannot complete a pending request or
override a mismatched `RMAPP`.

Firmware `8045e9e9` uses the complete path above for heterogeneous and
homogeneous profiles. `MODE=CAP|VOLT|RES` remains a supported set-all quick
action, but it is not a workaround for a row-profile transaction.

## Canonical firmware mixed M/MR/K frame

Firmware `8045e9e9` supplies an eight-character wire profile whose inactive
suffix is literal `N`. The Host accepts any valid active C/V/R prefix,
including an active prefix containing only one mode, and requires exactly one
matching `MR` for every active physical row.

The exact C source formatter schema is:

```text
M,seq=201,ts=201000,rows=5,cells=40,rgen=4,rrid=31,pgen=11,prid=62,profile=RVVCCVVR,fmt=mix1
MR,s=1,m=RES,unit=ohm,scale=-3,valid=FF,fresh=FF,error=00,fmt=mohm-x,D=10025000,...,...
MR,s=2,m=VOLT,unit=V,scale=-6,valid=FF,fresh=FF,error=00,fmt=uv-x,D=-1250,...,...
MR,s=3,m=VOLT,unit=V,scale=-6,valid=FF,fresh=FF,error=00,fmt=uv-x,D=...,...
MR,s=4,m=CAP,unit=pF,scale=-6,valid=FF,fresh=FF,error=00,fmt=pf6,D=6315000,...,...
MR,s=5,m=CAP,unit=pF,scale=-6,valid=FF,fresh=FF,error=00,fmt=pf6,D=...,...
K,seq=201,rgen=4,rrid=31,pgen=11,prid=62,crc=<8 hexadecimal digits>
```

Header identities have distinct meanings:

- `rgen/rrid` identify the active ROWS/configuration state;
- `pgen/prid` identify the applied row-mode profile;
- `profile` always contains all eight saved row modes, even when `rows < 8`.

Each one-based `MR.s` identifies a physical S row and `D=` contains exactly
eight comma-separated signed decimal or `Xhh` tokens. Its single-letter `m`
must match `profile[s-1]`; its unit, scale, and format must be:

| `m` | Host mode | Unit | Scale | `fmt` |
| --- | --- | --- | --- | --- |
| `C` | CAP | `pF` | `-6` | `pf6` |
| `V` | VOLT | `V` | `-6` | `uv-x` |
| `R` | RES | `ohm` | `-3` | `mohm-x` |

Canonical `8045e9e9` mixed rows do not carry PGA, reference, rail, or age
fields. They remain optional in the typed row model for legacy Replay and
future additive metadata, but the Host never infers or fabricates them when
the canonical frame omits them.

The parser accepts a frame only after all of these checks pass:

- one M header, then exactly one MR for every physical row S1 through
  S`rows`, in ascending order, then one K trailer;
- no duplicate, missing, inactive, or reordered rows;
- `cells=rows*8`, `fmt=mix1`, and a valid eight-character wire profile with an
  exact inactive `N` suffix; the active prefix need not be heterogeneous;
- row mode/profile, unit/scale/format, eight-value, mask, and `Xhh` consistency;
- matching M/K sequence, ROWS identities, and profile identities;
- CRC over exact M and ordered MR bytes including LF, excluding K.

Only then is a typed `MixedMeasurementFrame` emitted. It contains separate
`RowMeasurement` objects; there is no synthetic `unit="mixed"`. An incomplete,
duplicate-row, profile-mismatched, or bad-CRC frame is atomic rejection and
cannot partially update MatrixStore.

The Host additionally reads the former long-key/pipe-separated Replay schema as
a compatibility alias, but it never emits that alias as canonical `8045e9e9`
traffic. Any future divergence is resolved against firmware formatter source,
not patched independently in React.

## ROWS transaction and geometry

`ROWS=n` supports every integer in `1..8`. `RCMD` acknowledges acceptance; only
a matching ROWS `RAPP` confirms application at its frame boundary. `frame.rows`
is the snapshot and heatmap geometry authority. The row-mode profile always
retains eight entries, so inactive S rows recover their saved modes if ROWS is
later increased.

ROWS, global MODE, row profile, and deprecated rail configuration are separate
transaction types even if firmware records reuse an `RAPP` tag. Correlation
uses typed event plus request ID and expected values, never the tag alone.

## Rail telemetry and debug compatibility

Normal `MODE=VOLT` sends only `MODE=VOLT`; a profile containing VOLT sends only
one `ROWMODES=...`. Neither production path sends or waits for `RAILCFG`.
Firmware owns the automatic internal rail monitor.

The host parses `ARL`/`RAIL` telemetry into a typed `RailTelemetry`:

```text
railSpanUv, valid, fresh, age/ageMs, source, reason, timestamp
```

The normal UI shows the analogue span `AVDD - AVSS` as read-only telemetry. It
does not reinterpret that span as separate rails or present editable AVDD/AVSS
fields.

Legacy `RAILCFG`, `RACK`, rail `RAPP`, `RERR`, the `/api/measurement/rail`
endpoint, parser support, and old setup-profile fields remain available for
explicit debug/raw-command and replay compatibility. The endpoint is
deprecated and is never invoked automatically by a normal measurement request.

## Battery latest-attempt and last-good state

Battery tags such as `ABAT` and `AB50` are parsed into typed telemetry. A
measurement attempt and a displayable last-good voltage are separate:

```text
latestBatteryAttempt
lastGoodBattery
```

Firmware `8045e9e9` publishes `lastGoodMv`, `lastGoodValid`, `lastGoodFresh`,
`lastGoodAgeMs`, and `lastGoodFrame`; those fields are authoritative. The Host
also reads the earlier short `bl*` aliases. If neither schema is present, it
retains the last valid measurement seen in the current device session. For
example:

```text
ABAT,bt=-1,valid=0,fresh=0,ageMs=12,lastGoodMv=4092,lastGoodValid=1,lastGoodFresh=0,lastGoodAgeMs=1800,lastGoodFrame=77,...,reason=adc_timeout
```

produces a failed latest attempt and a 4.092 V last-known value. It must not
produce zero or erase the voltage. The next fresh valid sample replaces the
last-good value. A device identity change clears both caches; a transient gap
or reconnect to the same identity retains the value but marks it stale.

## Compatibility tests

Offline host fixtures cover established CAP/VOLT/RES parsing and the target
row/mixed contract. Required cases include:

- fragmented/single FF11 accepted events and fragmented FF30 applied events;
- wrong-ID MAPP/RMAPP rejection and no data-frame completion;
- RMACK/RMAPP/RMERR and timeout behavior;
- valid, incomplete, duplicate-row, profile-mismatched, and bad-CRC M/MR/K;
- ROWS and setup profiles for all integers 1 through 8;
- typed row identity, per-domain stores/history, and snapshot metadata;
- active-row colour masks, single-value RES, signed VOLT, and CAP delta;
- firmware/host battery last-good behavior and device-session reset;
- normal VOLT and row-profile commands emitting no `RAILCFG`;
- old rail config/profile and all legacy CAP fixtures remaining readable.

Cross-repository review and exact-source fixtures must compare
sequence, geometry, identities, profile, row modes/units/scales, values, masks,
errors, formats, and CRC acceptance. Replay proves the Host pipeline, while
real BLE evidence remains a separately reported HIL result.
