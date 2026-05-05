from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    project_root = Path(__file__).resolve().parent
    viewer_dir = project_root / "matrix_log_viewer"
    if not viewer_dir.exists():
        raise RuntimeError(f"Matrix viewer directory not found: {viewer_dir}")

    # Keep the root-level entry point simple: python main.py opens the GUI launcher.
    sys.path.insert(0, str(viewer_dir))
    from run_gui import main as run_gui_main

    return run_gui_main()


if __name__ == "__main__":
    raise SystemExit(main())
