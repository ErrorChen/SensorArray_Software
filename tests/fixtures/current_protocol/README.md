# Current firmware protocol fixtures

These packets mirror the production formatter in firmware commit
`22b7709e70ba0fdf59e6e947624139146cc4bac0`:

- `main/output/sensorarrayTextProtocol.c` for V/R, D, packed P and K ordering;
- `tools/text_protocol.py` and `tools/test_text_protocol.py` for the independent
  reference parser and CRC golden behaviour.

The CRC32 covers each header, D and P line including its LF and excludes K.
Keep these fixtures local to the desktop repository so its normal test suite
does not depend on a sibling firmware checkout or network access.
