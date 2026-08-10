# Host measurement protocol compatibility notes

This document records how SensorArray Desktop Host consumes the current
firmware protocol. It is an implementation/compatibility guide, not an
independent wire-protocol specification. The SensorArray firmware repository is
authoritative; in particular, its production formatter and command
implementation plus golden/reference tests outrank prose documentation.

The host keeps legacy import and parser plugins where required, but the modern
firmware ASCII measurement path is the `C|V|R` frame family described here.
Modern `V` and `R` frames must not be routed through legacy binary voltage or
MAT parsers.

## Stream and routing

All live and replay transports carry the same logical channels:

```text
DATA -> measurement packets
LOG  -> telemetry and asynchronous events
CTRL -> command request/response
```

Routing is by payload content. A complete `C`, `V`, or `R` packet received on a
LOG characteristic still enters the measurement assembler. BLE reassembly and
its envelope length/CRC checks happen before protocol routing.

The protocol is line-oriented ASCII. Production frames use LF (`\n`). A
measurement packet starts with one header, contains all required chunks in
contiguous index order, and ends at its `K` trailer. The assembler derives the
required chunk count from `cells`, rejects the entire malformed frame without
updating state, and must resynchronize on the next header.

Firmware accepts `rows=1..8`; `cells` and `n` must both equal `rows * 8`.
Compatibility and GUI acceptance explicitly exercise rows 1, 2, 4, and 8.
Each `D` or `P` chunk represents at most 16 cells, so the required chunk count
is `ceil(cells / 16)`. The final chunk may be short.

The assembler rejects at least:

- missing, duplicate, out-of-order, or extra `D` chunks;
- missing, duplicate, out-of-order, or extra `P` chunks in VOLT/RES;
- wrong value count or a non-contiguous chunk index;
- header/trailer `seq`, `gen`, or `rid` disagreement;
- malformed fixed values, malformed `Xhh`, or malformed packed PGA data;
- CRC mismatch.

No values from a rejected frame are committed. The diagnostic is preserved for
Status/Raw Log, and a following correct frame can still be parsed.

## CAP frame

Production CAP output has this structure:

```text
C,seq=...,ts=...,rows=...,cells=...,gen=...,rid=...,rf=...,pf=...,sf=...,bad=.../.../...,fmt=pf6,n=...
D0,<fixed-pF values, up to 16>
D1,...
...
K,seq=...,gen=...,rid=...,crc=<8 hexadecimal digits>
```

The `D` values are pF scaled by 1,000,000. The current invalid sentinel is
`-1000000`; it is detected before conversion and never treated as a real
-1 pF value. Existing circuit correction, per-cell user offset, baseline, and
Delta C/C0 % remain CAP-specific host operations.

`rf`, `pf`, and `sf` are row/primary/secondary freshness masks. `bad` contains
the frame stale, mixed-epoch, and invalid-count diagnostics. These fields remain
part of the CAP compatibility contract.

### CAP generation caveat

The `gen` and `rid` fields in a `C` header/trailer are the ROWS/configuration
snapshot generation/request ID. They are not measurement-mode generation and
request ID. After a matching `MAPP` applies CAP, the authoritative mode boundary
is `MAPP.seq` plus the `C` frame tag. The host rejects older pre-boundary CAP
frames but does not compare `C.gen/rid` to the mode transaction.

This is deliberately different from modern VOLT/RES frames, whose `gen/rid`
are measurement-mode state.

## VOLT and RES frames

Production VOLT/RES output has this structure:

```text
V|R,seq=...,ts=...,rows=...,cells=...,gen=...,rid=...,mode=...,unit=...,scale=...,valid=...,fresh=...,error=...,ref=...,rail=...,age=...,avdd=...,avss=...,vexc=...,rref=...,dur=...,tr=...,gc=...,ov=...,aa=...,fb=...,ir=...,to=...,st=...,spi=...,fmt=...,n=...,bad=...
D0,<signed integer or Xhh>,...
...
P0,<two packed hex digits per cell>
...
K,seq=...,gen=...,rid=...,crc=<8 hexadecimal digits>
```

The host retains all header fields, including fields not needed for immediate
value conversion:

| Field | Meaning |
| --- | --- |
| `seq`, `ts` | frame sequence and firmware timestamp in microseconds |
| `rows`, `cells`, `n` | active geometry and declared value count |
| `gen`, `rid` | applied measurement-mode generation and request ID |
| `mode`, `unit`, `scale`, `fmt` | explicit quantity contract |
| `valid`, `fresh`, `error` | independent 64-bit cell masks |
| `ref` | ADS reference source |
| `rail`, `age`, `avdd`, `avss` | rail validity, age in frames, and uV snapshot |
| `vexc`, `rref` | matrix reference/excitation uV and reference resistor ohms |
| `dur`, `tr` | frame and transition duration in us |
| `gc`, `ov` | gain-change and overrange counts |
| `aa`, `fb` | autorange attempts and fallback count |
| `ir` | bounded I/O retries that recovered this frame |
| `to`, `st`, `spi` | timeout, stale, and SPI diagnostic counts |
| `bad` | invalid-cell count |

`ir` is a recovered retry diagnostic; it is not an invalid flag.

### Quantity and scale

The quantity is selected only from the header contract, never from numeric
magnitude:

| Frame | Header contract | Raw `D` integer | Physical host value |
| --- | --- | --- | --- |
| VOLT | `V`, `mode=VOLT`, `unit=V`, `scale=-6`, `fmt=uv-x` | microvolts | `raw * 10^-6 V` |
| RES | `R`, `mode=RES`, `unit=ohm`, `scale=-3`, `fmt=mohm-x` | milliohms | `raw * 10^-3 ohm` |

Signed decimal input is valid. For example, VOLT raw `-1250` is -0.00125 V,
not an error marker. The UI may select engineering prefixes such as uV, mV,
V, mOhm, ohm, kOhm, or MOhm without changing the stored quantity.

### Valid, fresh, and error semantics

`valid`, `fresh`, and `error` are separate bit masks and are parsed independently
from the textual `D` token. A cell can be invalid yet fresh, or valid but stale.
The host preserves the exact combination for every active cell.

- Valid and fresh finite values participate in live display and trends.
- Invalid values are `null` in the heatmap and never become zero.
- Stale values remain diagnosable but do not enter the valid live series,
  baseline/statistics, or automatic colour range.
- Inactive cells outside `rows * 8` are `null`; high unused mask bits are ignored.
- Error code/reason is exported and shown in tooltips/status.

### `Xhh` cell errors

When a VOLT/RES cell is not valid, its `D` token is `Xhh`, where `hh` is an
unsigned hexadecimal firmware error code. The host preserves the numeric code,
the raw token, and a human-readable reason:

| Code | Firmware name | Host meaning |
| --- | --- | --- |
| `00` | `ok` | no reported cell error |
| `01` | `route` | route/configuration failure |
| `02` | `spi` | SPI failure |
| `03` | `timeout` | DRDY timeout |
| `04` | `stale` | stale conversion |
| `05` | `ref_alarm` | reference alarm |
| `06` | `pga_abs` | PGA absolute-input alarm |
| `07` | `pga_diff` | PGA differential-input alarm |
| `08` | `saturated` | saturated measurement |
| `09` | `common_mode` | common-mode violation |
| `0A` | `rail` | rail invalid |
| `0B` | `reference` | reference invalid |
| `0C` | `denominator` | denominator too close to zero |
| `0D` | `open` | open circuit |
| `0E` | `short` | short circuit |
| `0F` | `negative` | invalid negative resistance/result |
| `10` | `range` | out of supported range |
| `11` | `overflow` | conversion overflow |
| `12` | `unstable` | unstable sample set |
| `13` | `autorange` | autorange failure |
| `14` | `unsupported` | unsupported measurement/path |
| `15` | `readback` | route/register readback mismatch |

Any other two-digit code is retained and displayed as
`Unknown firmware cell error 0xHH`. An unknown code must not crash parsing or
default to zero.

### Packed PGA chunks

PGA data is not comma-separated. Each `P<n>,` line contains exactly two
hexadecimal digits per cell, packed continuously, for example:

```text
P0,01020408102000010204081020000102
```

The chunk covers the same cell range as `D<n>`. Literal values are:

| Wire | Presentation |
| --- | --- |
| `01` | PGA x1 |
| `02` | PGA x2 |
| `04` | PGA x4 |
| `08` | PGA x8 |
| `10` | PGA x16 |
| `20` | PGA x32 |
| `00` | verified PGA bypass |

`00` is not missing data and must not be rendered as x0 or unknown.

## CRC-32

The firmware computes the reflected CRC-32 implemented by its production
formatter. The CRC input is the exact encoded bytes of every line before `K`,
including the LF after each line and excluding the `K` line itself:

```text
CAP:  C header + all D lines
V/R:  V or R header + all D lines + all P lines
```

The host must not reuse a CAP-only accumulator that omits `P`. `K.seq/gen/rid`
must match the header in addition to the CRC. A mismatch rejects the complete
frame, leaves MatrixStore unchanged, records a parser diagnostic, and resets
the assembler so the next header can recover.

## Measurement-mode transaction

Commands are asynchronous frame-boundary transactions:

```text
MODE=CAP|VOLT|RES
MACK,id=<requestId>,old=<mode>,new=<mode>,state=accepted
MAPP,id=<requestId>,gen=<generation>,old=<mode>,new=<mode>,seq=<frameSeq>,state=applied,transitionUs=<us>
```

The host state machine is strict:

1. Send one mode request.
2. Correlate `MACK` by request ID and requested mode. `MACK` means queued only.
3. Keep `appliedMode` unchanged and expose `pendingMode`/waiting state.
4. Commit only on matching `MAPP.id`; retain `gen`, `rid`, and boundary `seq`.
5. Reject older frames after the boundary. For VOLT/RES, also require frame
   tag/mode and mode `gen/rid`; for CAP use the boundary rule above.
6. On timeout, `ERR`, or `MERR`, retain the last historical confirmation but
   expose the transition error and actual device SAFE/DEGRADED state. Never
   claim that the old quantity is still active solely because it was last seen.

A same-mode request can still create a new generation and must follow the same
transaction. A queued command may also be superseded or dropped in the bounded
firmware mailbox, which is why an explicit host timeout is required.

If transition application fails, current firmware emits `MERR` and moves its
active mode to `NONE` with a SAFE state. This production behavior takes
precedence over any older prose implying the old mode remains active.

## External rail transaction for VOLT

VOLT requires an externally measured rail split. Measurements must be taken
with a DMM against GND under the current supply, wiring, and load:

```text
RAILCFG=<AVDD_uV>,<negative_AVSS_uV>
RACK,id=...,avdd=...,avss=...,source=external,state=accepted
RAPP,id=...,gen=...,seq=...,avdd=...,avss=...,source=external,state=applied
```

Firmware validation requires AVDD to be positive, AVSS negative, and total
span to be within the supported 3,500,000 to 6,000,000 uV range. `RAILCFG` is
rejected in VOLT with `ERR,cmd=RAILCFG,reason=apply_before_volt`.

The host workflow is therefore:

```text
CAP or RES
 -> obtain paired current external DMM readings
 -> RAILCFG in integer uV
 -> matching RACK (accepted)
 -> matching rail RAPP (applied)
 -> MODE=VOLT
 -> matching MACK
 -> matching MAPP
 -> first matching CRC-valid VOLT frame
```

The host never invents defaults. Nominal supply values, battery voltage, and
the ADS supply-monitor rail are not substitutes for external DMM measurements.
RES does not require `RAILCFG`.

`RAPP` is overloaded by firmware. A rail transaction is identified by
`source=external,avdd,avss,state=applied`; a ROWS transaction carries
`old,new,status=applied`. Correlation uses event type plus request ID, not tag
alone.

## ROWS transaction

`ROWS=n` supports `n=1..8`. `RCMD` acknowledges acceptance; only matching
`RAPP` confirms application at its frame boundary. Matrix geometry is then
verified against complete frames. Mode and ROWS transactions use different
generation semantics even though both produce frames with `gen/rid` fields.

## ADS identity and diagnostics

`ADS?` returns the actual identity/validity state. In particular:

```text
ADS,chip=unknown,valid=0,...
```

means **ADS identity unconfirmed**. The host must not replace it with ADS1262
based on expected board population.

`ADSCHK[=<samples>]` produces an accepted command response followed by
asynchronous `ADSCHK` and `ADSCHKSTAT` records. The host correlates all of them
by request ID and exposes checking/completed/failed state plus the firmware
fields for sample count, fresh count, timing/period, SPI/DRDY/errors, changed
samples, and restore result. Restore failure can also produce `MFAULT` and is
not swallowed as a generic log line.

Unknown log tags remain visible as `Unknown firmware log (TAG)` and never stop
the parser.

## Battery telemetry

The current battery command/log family includes:

- `BAT?` -> current `ABAT` cache snapshot.
- `BATNOW` -> accepted `ACK`, then asynchronous `BAPP`; query `BAT?` for the
  resulting full snapshot rather than assuming `ACK` or `BAPP` contains it.
- `BATD` -> accepted diagnostic transaction and `BATD`/`BAPP` outcome.
- `BATPERIOD?` and `BATPERIOD=...` -> scheduler state/configuration.
- `ABAT` -> current battery snapshot.
- `AB50` -> periodic battery scheduler/result summary.

The host retains voltage, `valid`, `fresh`, `ageMs`, reason, restore state,
rail validity, run counters, retry, unstable, timeout, `spreadRaw`,
`spreadMaxRaw`, `validRun`, and `invalidRun` where present. Invalid battery
voltage is not converted into a real zero value. No state-of-charge percentage
is inferred without a chemistry/SOC model.

In `AB50`, `bs` describes battery freshness. A separate later ADS `fresh`
field describes the ADS cache and must not overwrite battery freshness.

## Rate telemetry

Firmware exposes several rates with different meanings:

- capture/physical rate (`cfps` or the corresponding physical capture field);
- emitted measurement-frame rate (`efps`);
- per-sink output/packet rate (`ofps` for USB/Serial, BLE, and Wi-Fi views);
- configured target/budget rate.

`OUTCAP` and sink `ofps` are output policy/throughput, not acquisition FPS.
The Status UI labels Capture, Emitted, Serial, BLE, Wi-Fi, and Target
independently and does not substitute one for another.

## Transport constants and applied-event limitation

Current transport constants are unchanged:

| Transport | Contract |
| --- | --- |
| Serial | 115200 baud, shared line protocol |
| BLE | service `00FF`; `FF10` CTRL RX, `FF11` CTRL TX, `FF20` DATA, `FF30` LOG |
| Wi-Fi UDP | DATA 3333, LOG 3334, CTRL 3335 |
| Replay | same registry/parser/store/snapshot/UI path as live data |

There is an important current firmware limitation. Accepted responses are
returned to the initiating command transport, but frame-boundary applied events
(`MAPP`, rail/rows `RAPP`, `ADSCHK`, `ADSCHKSTAT`, and `BAPP`) are emitted by the
production Serial event path and are not broadcast on BLE `FF30` or Wi-Fi LOG.

Consequences:

- Serial-only GUI transactions can be fully correlated.
- A BLE-only or Wi-Fi-only strict GUI can observe `MACK`/`RACK`/`ACK`, but it
  cannot prove application; it must remain pending and time out.
- The host must not infer application from a `V`, `R`, or later CAP frame,
  because the required transaction event is the matching `MAPP`/`RAPP`.
- Full BLE/Wi-Fi transaction HIL currently needs a Serial observation sidecar,
  or a future firmware change that publishes applied events to those LOG paths.

This limitation must be reported as a firmware capability BLOCKED/FAIL result,
not hidden by Replay, optimistic UI state, or a data-frame heuristic.

## Host compatibility tests

Firmware-derived fixtures are kept in the host test tree so routine tests do
not depend on GitHub or a sibling checkout. Compatibility tests cover CAP,
VOLT, RES, signed voltage, `Xhh`, unknown error codes, all PGA literals,
dynamic rows, CRC scope including `P`, malformed-frame recovery, MACK/MAPP,
generation rejection, rail/ADS/battery transactions, Replay end to end, and
session round trips.

When a sibling firmware checkout is available, cross-repository checks should
feed the same bytes through the firmware reference parser and desktop parser
and compare mode, sequence, geometry, generation/request ID, unit/scale,
values, masks, error codes, PGA, and CRC acceptance.
