from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    project_root = Path(__file__).resolve().parent
    viewer_dir = project_root / "matrix_log_viewer"
    if not viewer_dir.exists():
        raise RuntimeError(f"Matrix viewer directory not found: {viewer_dir}")

    # Default entry point: launch the Dash viewer. COM ports can be selected in the web UI.
    sys.path.insert(0, str(viewer_dir))
    from run_viewer import main as run_viewer_main

    return run_viewer_main()


if __name__ == "__main__":
    raise SystemExit(main())
