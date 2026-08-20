# Current firmware protocol fixtures

These packets mirror the production formatter in firmware commit
`8045e9e9ec9599533c52c15dfcb6002f79fd15f1`:

- `main/output/sensorarrayTextProtocol.c` for V/R, D, packed P and K ordering;
- `tools/text_protocol.py` and `tools/test_text_protocol.py` for the independent
  reference parser and CRC golden behaviour.

The CRC32 covers each header, D and P line including its LF and excludes K.
Keep these fixtures local to the desktop repository so its normal test suite
does not depend on a sibling firmware checkout or network access.
