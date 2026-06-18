from __future__ import annotations

import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[1]
    src = root / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    from sensorarray_app.app.bootstrap import main as app_main

    args = list(sys.argv[1:] if argv is None else argv)
    translated: list[str] = []
    index = 0
    while index < len(args):
        item = args[index]
        if item == "--port":
            translated.append("--serial-port")
        elif item == "--baud":
            translated.append("--serial-baud")
        elif item in {"--port-web", "--host", "--debug"}:
            translated.append(item)
        elif item in {"--input-mode", "--read-size", "--auto-reconnect", "--max-points", "--save-csv", "--replay-file", "--replay-speed", "--no-browser"}:
            if item not in {"--auto-reconnect", "--no-browser"} and index + 1 < len(args):
                index += 1
            index += 1
            continue
        else:
            translated.append(item)
        if item not in {"--debug"} and index + 1 < len(args) and not args[index + 1].startswith("--"):
            index += 1
            translated.append(args[index])
        index += 1
    return app_main(translated)


if __name__ == "__main__":
    raise SystemExit(main())
