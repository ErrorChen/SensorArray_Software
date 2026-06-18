from __future__ import annotations


def make_snapshot(runtime) -> dict:
    return runtime.snapshot()
