"""Widget-cap negotiation in client.hello."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gateway.platforms.desktop_app import _register_client_hello


@pytest.fixture(autouse=True)
def _reset_handler(monkeypatch):
    """Force a fresh registration so each test sees the latest handler body.

    Captures the original handler before the test (if any) and restores it
    after, so the methods dict does not leak modified state across tests in
    the same xdist worker.
    """
    from gateway.platforms import desktop_app
    from tui_gateway import server as tg_server

    original_handler = tg_server._methods.get("client.hello")
    monkeypatch.setattr(desktop_app, "_HELLO_REGISTERED", False)
    tg_server._methods.pop("client.hello", None)
    yield
    if original_handler is None:
        tg_server._methods.pop("client.hello", None)
    else:
        tg_server._methods["client.hello"] = original_handler


def test_server_advertises_widget_render():
    from tui_gateway import server as tg_server

    _register_client_hello()
    handler = tg_server._methods["client.hello"]
    resp = handler(1, {})
    assert "widget.render" in resp["result"]["capabilities"]


def test_records_client_widget_render_on_transport(monkeypatch):
    from tui_gateway import server as tg_server

    _register_client_hello()
    handler = tg_server._methods["client.hello"]
    fake_transport = MagicMock()
    monkeypatch.setattr(tg_server, "current_transport", lambda: fake_transport)

    handler(1, {"client_id": "tauri-test-client", "capabilities": ["widget.render"]})

    assert getattr(fake_transport, "client_capabilities", None) == ["widget.render"]


def test_no_transport_does_not_crash(monkeypatch):
    from tui_gateway import server as tg_server

    _register_client_hello()
    handler = tg_server._methods["client.hello"]
    monkeypatch.setattr(tg_server, "current_transport", lambda: None)

    resp = handler(1, {"capabilities": ["widget.render"]})
    assert "result" in resp
