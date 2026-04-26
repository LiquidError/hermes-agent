"""``GET /health`` reports adapter state and paired clients.

Tauri uses this to render connection status without paying for a full
WebSocket handshake.
"""

from __future__ import annotations

import socket

import pytest
import pytest_asyncio
from aiohttp import ClientSession

from gateway.config import PlatformConfig
from gateway.platforms.desktop_app import DesktopAppAdapter
from gateway.platforms.desktop_app_auth import TokenStore


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest_asyncio.fixture
async def adapter_with_clients(tmp_path):
    token_file = tmp_path / "tokens.json"
    store = TokenStore(token_file)
    store.add("client-a", "tok-a")
    store.add("client-b", "tok-b")
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
    assert await adapter.connect()
    try:
        yield port, token_file
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_health_lists_paired_clients(adapter_with_clients):
    port, _ = adapter_with_clients
    async with ClientSession() as s:
        async with s.get(f"http://127.0.0.1:{port}/health") as r:
            assert r.status == 200
            body = await r.json()

    assert body["platform"] == "desktop_app"
    names = sorted(c["name"] for c in body["paired_clients"])
    assert names == ["client-a", "client-b"]
    # Freshly paired — neither has connected yet.
    for c in body["paired_clients"]:
        assert c["last_seen_at"] is None


@pytest.mark.asyncio
async def test_health_does_not_expose_token_hashes(adapter_with_clients):
    port, _ = adapter_with_clients
    async with ClientSession() as s:
        async with s.get(f"http://127.0.0.1:{port}/health") as r:
            body = await r.json()

    for client in body["paired_clients"]:
        assert "token_hash" not in client
        assert "token" not in client


@pytest.mark.asyncio
async def test_health_reflects_recent_auth(adapter_with_clients):
    import time

    port, _ = adapter_with_clients

    before = time.time()
    async with ClientSession() as s:
        # Authenticate as client-a so its last_seen_at is updated.
        async with s.ws_connect(
            f"ws://127.0.0.1:{port}/ws",
            headers={"Authorization": "Bearer tok-a"},
        ):
            pass

        async with s.get(f"http://127.0.0.1:{port}/health") as r:
            body = await r.json()

    by_name = {c["name"]: c for c in body["paired_clients"]}
    assert by_name["client-a"]["last_seen_at"] is not None
    assert by_name["client-a"]["last_seen_at"] >= before
    assert by_name["client-b"]["last_seen_at"] is None
