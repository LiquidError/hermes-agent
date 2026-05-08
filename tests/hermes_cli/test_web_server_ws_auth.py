"""WebSocket upgrade auth tests for embedded chat surfaces."""

import pytest
from fastapi.testclient import TestClient

from hermes_cli import web_server


@pytest.fixture(autouse=True)
def enable_embedded_chat(monkeypatch):
    monkeypatch.setattr(web_server, "_DASHBOARD_EMBEDDED_CHAT_ENABLED", True, raising=False)
    web_server.app.state.bound_host = "127.0.0.1"
    yield


@pytest.fixture
def client():
    c = TestClient(web_server.app)
    c.headers["host"] = "127.0.0.1"
    return c


def test_ws_accepts_authorization_bearer_header(client):
    with client.websocket_connect(
        "/api/ws",
        headers={"authorization": f"Bearer {web_server._SESSION_TOKEN}"},
    ) as ws:
        ws.close()


def test_ws_accepts_session_header(client):
    with client.websocket_connect(
        "/api/ws",
        headers={web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN},
    ) as ws:
        ws.close()


def test_ws_accepts_legacy_query_token(client):
    with client.websocket_connect(
        f"/api/ws?token={web_server._SESSION_TOKEN}",
    ) as ws:
        ws.close()


def test_ws_off_loopback_accepts_api_server_key(client, monkeypatch):
    web_server.app.state.bound_host = "0.0.0.0"
    monkeypatch.setenv("API_SERVER_KEY", "x" * 32)
    with client.websocket_connect(
        "/api/ws",
        headers={"authorization": "Bearer " + ("x" * 32), "host": "host.ts.net"},
    ) as ws:
        ws.close()


def test_ws_rejects_missing_token(client):
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/api/ws"):
            pass
    assert exc_info.value.code == 4401


def test_ws_disabled_chat_returns_4403_not_4401(client, monkeypatch):
    """Disabled-chat 4403 must fire before auth check — no token-oracle leakage."""
    monkeypatch.setattr(web_server, "_DASHBOARD_EMBEDDED_CHAT_ENABLED", False)
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/api/ws"):
            pass
    assert exc_info.value.code == 4403
