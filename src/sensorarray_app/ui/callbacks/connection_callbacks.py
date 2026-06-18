from __future__ import annotations

from dash import Input, Output, State, callback_context


def register_connection_callbacks(app, runtime) -> None:
    @app.callback(
        Output("connection-state", "children"),
        Output("discovery-results", "children"),
        Input("snapshot-store", "data"),
    )
    def update_connection(snapshot):
        if not snapshot:
            return "DISCONNECTED", ""
        transport = snapshot["transport"]
        discovery = snapshot["discovery"]
        ble = ", ".join(f"{item.get('name') or '<unnamed>'} {item.get('address')} RSSI={item.get('rssi')}" for item in discovery["bleResults"][:4])
        wifi = ", ".join(f"{item.get('host')} {item.get('method')} confirmed={item.get('confirmed')}" for item in discovery["wifiResults"][:4])
        return f"{transport.get('transport')} {transport.get('state')} gen={transport.get('sessionGeneration')}", f"BLE: {ble or discovery['bleState']} | Wi-Fi: {wifi or discovery['wifiState']}"

    @app.callback(
        Output("connect-button", "n_clicks"),
        Output("disconnect-button", "n_clicks"),
        Output("ble-scan-button", "n_clicks"),
        Output("wifi-discover-button", "n_clicks"),
        Input("connect-button", "n_clicks"),
        Input("disconnect-button", "n_clicks"),
        Input("ble-scan-button", "n_clicks"),
        Input("wifi-discover-button", "n_clicks"),
        State("transport-selector", "value"),
        State("serial-port", "value"),
        State("serial-baud", "value"),
        State("replay-file", "value"),
        State("ble-address", "value"),
        State("wifi-host", "value"),
        prevent_initial_call=True,
    )
    def connection_actions(connect, disconnect, ble_scan, wifi_scan, transport, serial_port, serial_baud, replay_file, ble_address, wifi_host):
        trigger = callback_context.triggered_id
        try:
            if trigger == "connect-button":
                if transport == "serial":
                    runtime.connect_serial(serial_port, int(serial_baud or 115200), False)
                elif transport == "replay":
                    runtime.connect_replay(replay_file or "")
                elif transport == "ble":
                    runtime.connect_ble(ble_address or "")
                elif transport == "wifi":
                    runtime.connect_wifi(wifi_host or "192.168.4.1")
            elif trigger == "disconnect-button":
                runtime.disconnect()
            elif trigger == "ble-scan-button":
                runtime.start_ble_scan()
            elif trigger == "wifi-discover-button":
                runtime.start_wifi_scan()
        except Exception as exc:
            runtime._host_log("Connection", "error", str(exc))
        return 0, 0, 0, 0
