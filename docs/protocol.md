# b41 Protocol

Protocol baseline:

```text
b41c5256fbb5b23a0f0d98ed651db2f6ced3a0d6
```

Primary capacitance data is compact ASCII:

```text
C,seq=301,ts=123456789,rows=5,cells=40,gen=12,rid=9,rf=1F,pf=1F,sf=1F,bad=0/0/0,fmt=pf6,n=40
D0,<up to 16 fixed-point integers>
D1,<up to 16 fixed-point integers>
D2,<short final line is legal>
K,seq=301,gen=12,rid=9,crc=89ABCDEF
```

Rules:

- `rows` is 1..8.
- `cells == rows * 8`.
- `n == cells`.
- D lines start at D0 and are contiguous.
- D lines have at most 16 values.
- The final D line may be short.
- K seq/gen/rid must match C.
- CRC is reflected CRC-32 over C through D bytes, including LF, excluding K.
- ASCII decoding is strict.
- Invalid or CRC-failed frames do not enter the matrix store.

Capacitance:

```text
rawPf = rawFixed / 1_000_000.0
correctedPf = rawPf - 33.0
```

The invalid sentinel is `-1000000` and is detected before conversion.

ROWS:

- `RCMD` means the device accepted the request.
- `RAPP` means the device applied it.
- The following C frame must match the applied row count and generation.

BLE:

- Expected service: `00FF`.
- CTRL RX: `FF10` write.
- CTRL TX: `FF11` notify/indicate.
- DATA TX: `FF20` notify/indicate.
- LOG TX: `FF30` notify/indicate.
- If UUIDs differ, the backend reports available notify characteristics.

Wi-Fi UDP:

- DATA: 3333.
- LOG: 3334.
- CTRL: 3335.

