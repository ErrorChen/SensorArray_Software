from sensorarray_app.app.configuration import AppConfiguration
from sensorarray_backend.core.runtime import BackendRuntime
from sensorarray_backend.core.snapshot import snapshot_payload
from sensorarray_backend.core.websocket_metrics import WebSocketMetrics


def test_websocket_metrics_are_bounded_and_visible_in_snapshot():
    metrics = WebSocketMetrics()
    metrics.connected()
    metrics.connected()
    metrics.disconnected()
    metrics.disconnected()
    metrics.disconnected()
    assert metrics.snapshot() == {"activeSubscribers": 0, "acceptedConnections": 2}

    payload = snapshot_payload(BackendRuntime(AppConfiguration()))
    assert set(payload["performance"]["webSocket"]) == {"activeSubscribers", "acceptedConnections"}

