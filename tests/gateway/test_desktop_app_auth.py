"""TokenStore + WebSocket bearer-token middleware tests."""

from __future__ import annotations

import socket

import pytest
import pytest_asyncio
from aiohttp import ClientSession, WSServerHandshakeError

from gateway.config import PlatformConfig
from gateway.platforms.desktop_app import DesktopAppAdapter
from gateway.platforms.desktop_app_auth import TokenStore


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest_asyncio.fixture
async def auth_adapter(tmp_path):
    """Adapter listening on a free loopback port with one paired client
    (``client-a`` / ``valid-token``).
    """
    token_file = tmp_path / "tokens.json"
    store = TokenStore(token_file)
    store.add("client-a", "valid-token")
    store.save()

    port = _free_port()
    adapter = DesktopAppAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "host": "127.0.0.1",
                "port": port,
                "token_file": str(token_file),
            },
        ),
    )
    ok = await adapter.connect()
    assert ok, "adapter failed to start"
    try:
        yield port
    finally:
        await adapter.disconnect()


# ---------------------------------------------------------------------------
# TokenStore — file-backed bearer-token registry. Stores hashes, never
# plaintext. The plaintext bearer is shown to the user once at pair time
# and never recoverable from the store.
# ---------------------------------------------------------------------------


class TestTokenStore:
    def test_load_missing_file_is_empty(self, tmp_path):
        from gateway.platforms.desktop_app_auth import TokenStore

        store = TokenStore(tmp_path / "tokens.json")
        assert store.is_empty() is True
        assert store.list() == []

    def test_add_then_verify_returns_client_name(self, tmp_path):
        from gateway.platforms.desktop_app_auth import TokenStore

        store = TokenStore(tmp_path / "tokens.json")
        store.add("client-a", "bearer-secret-xyz")
        assert store.verify("bearer-secret-xyz") == "client-a"

    def test_verify_wrong_token_returns_none(self, tmp_path):
        from gateway.platforms.desktop_app_auth import TokenStore

        store = TokenStore(tmp_path / "tokens.json")
        store.add("client-a", "bearer-secret-xyz")
        assert store.verify("not-the-right-token") is None

    def test_verify_empty_token_returns_none(self, tmp_path):
        from gateway.platforms.desktop_app_auth import TokenStore

        store = TokenStore(tmp_path / "tokens.json")
        store.add("client-a", "bearer-secret-xyz")
        assert store.verify("") is None
        assert store.verify(None) is None

    def test_save_then_load_roundtrip(self, tmp_path):
        from gateway.platforms.desktop_app_auth import TokenStore

        path = tmp_path / "tokens.json"
        first = TokenStore(path)
        first.add("client-a", "tok1")
        first.save()

        second = TokenStore(path)
        assert second.verify("tok1") == "client-a"
        assert second.is_empty() is False

    def test_save_does_not_persist_plaintext(self, tmp_path):
        from gateway.platforms.desktop_app_auth import TokenStore

        path = tmp_path / "tokens.json"
        store = TokenStore(path)
        store.add("client-a", "the-very-secret-token")
        store.save()

        on_disk = path.read_text()
        assert "the-very-secret-token" not in on_disk

    def test_revoke_removes_client(self, tmp_path):
        from gateway.platforms.desktop_app_auth import TokenStore

        store = TokenStore(tmp_path / "tokens.json")
        store.add("client-a", "tok1")
        assert store.revoke("client-a") is True
        assert store.verify("tok1") is None
        assert store.is_empty() is True

    def test_revoke_unknown_returns_false(self, tmp_path):
        from gateway.platforms.desktop_app_auth import TokenStore

        store = TokenStore(tmp_path / "tokens.json")
        store.add("client-a", "tok1")
        assert store.revoke("never-paired") is False
        # didn't accidentally remove a different client
        assert store.verify("tok1") == "client-a"


# ---------------------------------------------------------------------------
# WS handshake bearer-token middleware
# ---------------------------------------------------------------------------


class TestHandshake:
    @pytest.mark.asyncio
    async def test_rejects_without_authorization_header(self, auth_adapter):
        port = auth_adapter
        async with ClientSession() as s:
            with pytest.raises(WSServerHandshakeError) as exc:
                await s.ws_connect(f"ws://127.0.0.1:{port}/ws")
            assert exc.value.status == 401

    @pytest.mark.asyncio
    async def test_rejects_wrong_token(self, auth_adapter):
        port = auth_adapter
        async with ClientSession() as s:
            with pytest.raises(WSServerHandshakeError) as exc:
                await s.ws_connect(
                    f"ws://127.0.0.1:{port}/ws",
                    headers={"Authorization": "Bearer wrong-token"},
                )
            assert exc.value.status == 401

    @pytest.mark.asyncio
    async def test_rejects_authorization_without_bearer_prefix(self, auth_adapter):
        port = auth_adapter
        async with ClientSession() as s:
            with pytest.raises(WSServerHandshakeError) as exc:
                await s.ws_connect(
                    f"ws://127.0.0.1:{port}/ws",
                    headers={"Authorization": "valid-token"},
                )
            assert exc.value.status == 401

    @pytest.mark.asyncio
    async def test_accepts_valid_token(self, auth_adapter):
        import json

        port = auth_adapter
        async with ClientSession() as s:
            async with s.ws_connect(
                f"ws://127.0.0.1:{port}/ws",
                headers={"Authorization": "Bearer valid-token"},
            ) as ws:
                ready = json.loads(await ws.receive_str())
                assert ready.get("method") == "event"
                assert ready["params"]["type"] == "gateway.ready"

    @pytest.mark.asyncio
    async def test_rejection_precedes_websocket_upgrade(self, auth_adapter):
        # Rejection must happen before ws.prepare(), so no RPC method
        # (not even client.hello) can run unauthenticated.
        port = auth_adapter
        async with ClientSession() as s:
            async with s.get(
                f"http://127.0.0.1:{port}/ws",
                headers={
                    "Authorization": "Bearer wrong-token",
                    "Connection": "Upgrade",
                    "Upgrade": "websocket",
                    "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
                    "Sec-WebSocket-Version": "13",
                },
            ) as resp:
                assert resp.status == 401
                assert resp.headers.get("WWW-Authenticate", "").startswith("Bearer")
