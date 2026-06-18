from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from dash import Dash, Input, Output

from sensorarray_app.app.configuration import AppConfiguration
from sensorarray_app.app.runtime import SensorArrayRuntime
from sensorarray_app.ui.callbacks.battery_callbacks import register_battery_callbacks
from sensorarray_app.ui.callbacks.connection_callbacks import register_connection_callbacks
from sensorarray_app.ui.callbacks.diagnostics_callbacks import register_diagnostics_callbacks
from sensorarray_app.ui.callbacks.heatmap_callbacks import register_heatmap_callbacks
from sensorarray_app.ui.callbacks.log_callbacks import register_log_callbacks
from sensorarray_app.ui.callbacks.selection_callbacks import register_selection_callbacks
from sensorarray_app.ui.callbacks.setup_callbacks import register_setup_callbacks
from sensorarray_app.ui.callbacks.trend_callbacks import register_trend_callbacks
from sensorarray_app.ui.layout import build_layout
from sensorarray_app.ui.snapshots import make_snapshot


def create_app(config: AppConfiguration | None = None) -> Dash:
    cfg = config or AppConfiguration()
    runtime = SensorArrayRuntime(cfg)
    runtime.start()
    assets = Path(__file__).resolve().parents[1] / "ui" / "assets"
    app = Dash(__name__, title="SensorArray b41 Matrix", assets_folder=str(assets))
    app.layout = build_layout()
    app._sensorarray_runtime = runtime

    @app.callback(Output("snapshot-store", "data"), Input("snapshot-interval", "n_intervals"))
    def publish_snapshot(_n):
        return make_snapshot(runtime)

    register_connection_callbacks(app, runtime)
    register_setup_callbacks(app, runtime)
    register_heatmap_callbacks(app, runtime)
    register_selection_callbacks(app, runtime)
    register_trend_callbacks(app, runtime)
    register_battery_callbacks(app, runtime)
    register_log_callbacks(app, runtime)
    register_diagnostics_callbacks(app, runtime)
    return app


def parse_args(argv: list[str] | None = None) -> AppConfiguration:
    parser = argparse.ArgumentParser(description="SensorArray b41 real-time matrix host")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port-web", type=int, default=8050)
    parser.add_argument("--serial-port", default="COM12")
    parser.add_argument("--serial-baud", type=int, default=115200)
    parser.add_argument("--history-frames", type=int, default=18_000)
    parser.add_argument("--max-log-lines", type=int, default=10_000)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)
    return AppConfiguration(
        host=args.host,
        port=args.port_web,
        serialPort=args.serial_port,
        serialBaud=args.serial_baud,
        historyFrames=args.history_frames,
        maxLogLines=args.max_log_lines,
        debug=args.debug,
    )


def main(argv: list[str] | None = None) -> int:
    config = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if config.debug else logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    app = create_app(config)
    try:
        app.run(host=config.host, port=config.port, debug=config.debug, use_reloader=False)
    finally:
        runtime = getattr(app, "_sensorarray_runtime", None)
        if runtime is not None:
            runtime.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
