from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


def test_benchmark_gui_pipeline_replays_fast_binary_sample_if_available():
    sample = Path("matrix_log_viewer/sample_logs/sample_fast_binary_pure_120fps.bin")
    if not sample.exists():
        pytest.skip(f"sample replay file not present: {sample}")

    result = subprocess.run(
        [
            sys.executable,
            "matrix_log_viewer/benchmark_gui_pipeline.py",
            "--replay-file",
            str(sample),
        ],
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert "parser/store fps:" in result.stdout
    assert "gui helpers: ok" in result.stdout
