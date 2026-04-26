"""Contract tests for DesktopAppAdapter.

Covers behaviors that don't need a real socket: client.hello shape,
the network-bind guard, the aiohttp ↔ tui_gateway shim, default
config resolution, and get_chat_info.
"""

from __future__ import annotations

from pathlib import Path
from typing import List
from unittest.mock import patch

import pytest
from aiohttp import WSMessage, WSMsgType

from gateway.config import Platform, PlatformConfig
from gateway.platforms.desktop_app import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    PROTOCOL_VERSION,
    WS_PATH,
    DesktopAppAdapter,
    _AioHttpWsShim,
    _WSDisc,
    _default_token_file,
    _register_client_hello,
    check_desktop_app_requirements,
)
from hermes_constants import get_hermes_home


# ---------------------------------------------------------------------------
# check_desktop_app_requirements
# ---------------------------------------------------------------------------


def test_check_requirements_returns_true_when_aiohttp_available():
    assert check_desktop_app_requirements() is True


@patch("gateway.platforms.desktop_app.AIOHTTP_AVAILABLE", False)
def test_check_requirements_returns_false_without_aiohttp():
    assert check_desktop_app_requirements() is False


# ---------------------------------------------------------------------------
# Module-level constants & paths
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_default_host_is_loopback(self):
        assert DEFAULT_HOST == "127.0.0.1"

    def test_default_port_is_8645(self):
        assert DEFAULT_PORT == 8645

    def test_ws_path(self):
        assert WS_PATH == "/ws"

    def test_protocol_version_is_int(self):
        # Bumping this is a wire-breaking change for desktop clients.
        assert isinstance(PROTOCOL_VERSION, int)
        assert PROTOCOL_VERSION >= 1

    def test_default_token_file_is_profile_safe(self):
        # Must resolve under get_hermes_home(), not Path.home()/".hermes",
        # so per-profile state stays isolated.
        token_file = _default_token_file()
        assert token_file.parent == Path(get_hermes_home())
        assert token_file.name == "desktop_app_tokens.json"


# ---------------------------------------------------------------------------
# client.hello registration
# ---------------------------------------------------------------------------


class TestClientHelloRegistration:
    def test_register_is_idempotent(self):
        from tui_gateway import server as tg_server

        _register_client_hello()
        first = tg_server._methods.get("client.hello")
        _register_client_hello()
        second = tg_server._methods.get("client.hello")

        assert first is not None
        assert first is second

    def test_returns_protocol_version_and_capabilities(self):
        from tui_gateway import server as tg_server

        _register_client_hello()
        handler = tg_server._methods["client.hello"]

        resp = handler(
            42,
            {
                "client_id": "tauri-test-client",
                "client_version": "0.1.0",
                "capabilities": ["voice.in", "voice.out"],
            },
        )

        assert resp["jsonrpc"] == "2.0"
        assert resp["id"] == 42
        assert "result" in resp

        result = resp["result"]
        assert result["protocol_version"] == PROTOCOL_VERSION
        assert "server_version" in result
        caps = result["capabilities"]
        for required in ("session.list", "slash.exec", "model.options", "approval"):
            assert required in caps, f"capabilities missing {required!r}"
        assert result["client_id"] == "tauri-test-client"
        assert result["client_version"] == "0.1.0"
        assert result["client_capabilities"] == ["voice.in", "voice.out"]

    def test_handles_empty_params(self):
        from tui_gateway import server as tg_server

        _register_client_hello()
        handler = tg_server._methods["client.hello"]

        resp = handler(1, {})

        assert "result" in resp
        assert resp["result"]["client_id"] == "unknown"
        assert resp["result"]["client_version"] == "unknown"
        assert resp["result"]["client_capabilities"] == []

    def test_advertises_attachment_redaction_and_event_methods(self):
        # The Tauri client introspects this list to decide which RPC
        # methods + event types it can rely on. Adding capabilities is
        # safe; removing one is a wire-protocol break.
        from tui_gateway import server as tg_server

        _register_client_hello()
        handler = tg_server._methods["client.hello"]
        caps = handler(1, {})["result"]["capabilities"]

        for required in (
            "attachment.upload",
            "config.reveal_secret",
            "message.complete",
            "tool.complete",
        ):
            assert required in caps, f"capabilities missing {required!r}"


# ---------------------------------------------------------------------------
# DesktopAppAdapter init — config / env / defaults
# ---------------------------------------------------------------------------


class TestAdapterInit:
    def test_defaults(self, monkeypatch):
        monkeypatch.delenv("DESKTOP_APP_HOST", raising=False)
        monkeypatch.delenv("DESKTOP_APP_PORT", raising=False)
        monkeypatch.delenv("DESKTOP_APP_TOKEN_FILE", raising=False)
        adapter = DesktopAppAdapter(PlatformConfig(enabled=True))
        assert adapter._host == DEFAULT_HOST
        assert adapter._port == DEFAULT_PORT
        assert adapter.platform == Platform.DESKTOP_APP

    def test_extra_overrides_env(self, monkeypatch):
        monkeypatch.setenv("DESKTOP_APP_HOST", "10.0.0.1")
        monkeypatch.setenv("DESKTOP_APP_PORT", "9000")
        adapter = DesktopAppAdapter(
            PlatformConfig(enabled=True, extra={"host": "100.64.0.1", "port": 9999}),
        )
        assert adapter._host == "100.64.0.1"
        assert adapter._port == 9999

    def test_env_used_when_no_extra(self, monkeypatch):
        monkeypatch.setenv("DESKTOP_APP_HOST", "10.0.0.2")
        monkeypatch.setenv("DESKTOP_APP_PORT", "9001")
        adapter = DesktopAppAdapter(PlatformConfig(enabled=True))
        assert adapter._host == "10.0.0.2"
        assert adapter._port == 9001


# ---------------------------------------------------------------------------
# Token file detection — drives the network-bind guard.
# ---------------------------------------------------------------------------


class TestHasAnyToken:
    def test_missing_file_returns_false(self, tmp_path):
        adapter = DesktopAppAdapter(
            PlatformConfig(
                enabled=True,
                extra={"token_file": str(tmp_path / "nope.json")},
            ),
        )
        assert adapter._has_any_token() is False

    def test_empty_file_returns_false(self, tmp_path):
        f = tmp_path / "tokens.json"
        f.write_text("")
        adapter = DesktopAppAdapter(
            PlatformConfig(enabled=True, extra={"token_file": str(f)}),
        )
        assert adapter._has_any_token() is False

    def test_tiny_file_returns_false(self, tmp_path):
        # Empty JSON array is what the pair CLI writes before any client
        # is added; it must not satisfy the bind guard.
        f = tmp_path / "tokens.json"
        f.write_text("[]")
        adapter = DesktopAppAdapter(
            PlatformConfig(enabled=True, extra={"token_file": str(f)}),
        )
        assert adapter._has_any_token() is False

    def test_populated_file_returns_true(self, tmp_path):
        from gateway.platforms.desktop_app_auth import TokenStore

        f = tmp_path / "tokens.json"
        store = TokenStore(f)
        store.add("client-a", "tok")
        store.save()
        adapter = DesktopAppAdapter(
            PlatformConfig(enabled=True, extra={"token_file": str(f)}),
        )
        assert adapter._has_any_token() is True


# ---------------------------------------------------------------------------
# Network bind guard — mirrors api_server's. No real port binding here.
# ---------------------------------------------------------------------------


class TestBindGuard:
    @pytest.mark.asyncio
    async def test_refuses_ipv4_wildcard_without_token_file(self, tmp_path):
        adapter = DesktopAppAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "host": "0.0.0.0",
                    "token_file": str(tmp_path / "nope.json"),
                },
            ),
        )
        result = await adapter.connect()
        assert result is False

    @pytest.mark.asyncio
    async def test_refuses_ipv6_wildcard_without_token_file(self, tmp_path):
        adapter = DesktopAppAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "host": "::",
                    "token_file": str(tmp_path / "nope.json"),
                },
            ),
        )
        result = await adapter.connect()
        assert result is False


# ---------------------------------------------------------------------------
# _AioHttpWsShim — duck-typed contract that handle_ws expects
# ---------------------------------------------------------------------------


class _FakeWs:
    """Minimal stand-in for aiohttp.web.WebSocketResponse."""

    def __init__(self, inbound: List[WSMessage]) -> None:
        self._inbound = list(inbound)
        self.sent: List[str] = []
        self.closed = False
        self._exception: Exception | None = None

    async def send_str(self, text: str) -> None:
        self.sent.append(text)

    async def receive(self) -> WSMessage:
        if not self._inbound:
            return WSMessage(type=WSMsgType.CLOSED, data=None, extra="")
        return self._inbound.pop(0)

    async def close(self) -> None:
        self.closed = True

    def exception(self):
        return self._exception


@pytest.mark.asyncio
async def test_shim_accept_is_noop():
    # aiohttp's route handler already called ws.prepare() before the
    # shim is built, so accept() has nothing to do.
    shim = _AioHttpWsShim(_FakeWs([]))
    result = await shim.accept()
    assert result is None


@pytest.mark.asyncio
async def test_shim_send_text_forwards_to_send_str():
    fake = _FakeWs([])
    shim = _AioHttpWsShim(fake)
    await shim.send_text('{"jsonrpc":"2.0"}')
    assert fake.sent == ['{"jsonrpc":"2.0"}']


@pytest.mark.asyncio
async def test_shim_receive_text_returns_text_payload():
    fake = _FakeWs([WSMessage(type=WSMsgType.TEXT, data="hello", extra="")])
    shim = _AioHttpWsShim(fake)
    assert await shim.receive_text() == "hello"


@pytest.mark.asyncio
async def test_shim_receive_text_skips_ping_pong_binary():
    fake = _FakeWs([
        WSMessage(type=WSMsgType.PING, data=b"", extra=""),
        WSMessage(type=WSMsgType.PONG, data=b"", extra=""),
        WSMessage(type=WSMsgType.BINARY, data=b"\x00\x01", extra=""),
        WSMessage(type=WSMsgType.TEXT, data="payload", extra=""),
    ])
    shim = _AioHttpWsShim(fake)
    assert await shim.receive_text() == "payload"


@pytest.mark.asyncio
async def test_shim_disconnect_raises_starlette_class():
    # handle_ws catches starlette.websockets.WebSocketDisconnect by
    # identity; the shim must raise that exact class so the catch fires.
    from starlette.websockets import WebSocketDisconnect

    assert _WSDisc is WebSocketDisconnect

    fake = _FakeWs([WSMessage(type=WSMsgType.CLOSE, data=None, extra="")])
    shim = _AioHttpWsShim(fake)
    with pytest.raises(WebSocketDisconnect):
        await shim.receive_text()


@pytest.mark.asyncio
async def test_shim_error_msg_raises_disconnect():
    fake = _FakeWs([WSMessage(type=WSMsgType.ERROR, data=None, extra="")])
    shim = _AioHttpWsShim(fake)
    with pytest.raises(_WSDisc):
        await shim.receive_text()


@pytest.mark.asyncio
async def test_shim_close_calls_underlying_close():
    fake = _FakeWs([])
    shim = _AioHttpWsShim(fake)
    await shim.close()
    assert fake.closed is True


# ---------------------------------------------------------------------------
# send() — desktop_app routes through tui_gateway, never via base.send()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_returns_unsupported_error():
    adapter = DesktopAppAdapter(PlatformConfig(enabled=True))
    result = await adapter.send(chat_id="anything", content="hi")
    assert result.success is False
    assert result.message_id is None
    assert result.error == "send_not_supported_on_desktop_app"


# ---------------------------------------------------------------------------
# get_chat_info — abstract on BasePlatformAdapter, every adapter must
# implement it. Without this, the gateway factory can't even instantiate
# DesktopAppAdapter (TypeError on the abstract method).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_chat_info_returns_minimal_shape():
    adapter = DesktopAppAdapter(
        PlatformConfig(enabled=True, extra={"host": "127.0.0.1", "port": 8645}),
    )
    info = await adapter.get_chat_info("any-chat-id")
    # Base contract: at least {name, type}.
    assert "name" in info
    assert "type" in info
    # And the host/port the adapter is bound to, so /platforms can show it.
    assert info["host"] == "127.0.0.1"
    assert info["port"] == 8645
