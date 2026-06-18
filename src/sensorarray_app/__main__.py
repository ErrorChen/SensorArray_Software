from __future__ import annotations

import sys

from .app.bootstrap import main


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
