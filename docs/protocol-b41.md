# b41 Protocol Notes

This host targets SensorArray firmware commit:

```text
b41c5256fbb5b23a0f0d98ed651db2f6ced3a0d6
```

Main matrix DATA is C/D/K ASCII with dynamic `rows * 8` values. Legacy FAST_BINARY is voltage-only compatibility and must not be used as capacitance.

See `README.md` and `docs/architecture.md` for parser rules, CRC range, fixed-point conversion, ROWS, BLE, Wi-Fi, and battery logs.
