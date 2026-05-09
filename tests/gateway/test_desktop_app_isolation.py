"""Per-connection dispatcher isolation through DesktopAppAdapter.

Two paired clients connecting concurrently must not be able to see or
mutate each other's in-flight session state. Persisted sessions in
state.db remain shared (cross-platform continuity); only the runtime
state held in `_DispatcherState.sessions` is isolated.
"""

from __future__ import annotations

import asyncio
import json
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
async def two_client_adapter(tmp_path):
    token_file = tmp_path / "tokens.json"
    store = TokenStore(token_file)
    store.add("client-a", "token-a")
    store.add("client-b", "token-b")
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
        yield port
    finally:
        await adapter.disconnect()


async def _rpc(ws, rid: int, method: str, params: dict | None = None) -> dict:
    """Send a request, drain events until the matching response arrives.

    Bounded by ``asyncio.wait_for`` so a missed response surfaces as a
    deterministic failure instead of stalling the test worker forever.
    """
    await ws.send_json({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
    while True:
        msg = json.loads(await asyncio.wait_for(ws.receive_str(), timeout=5))
        if msg.get("id") == rid:
            return msg


class TestPerConnectionIsolation:
    @pytest.mark.asyncio
    async def test_session_close_does_not_cross_connections(self, two_client_adapter):
        port = two_client_adapter
        async with ClientSession() as s:
            async with s.ws_connect(
                f"ws://127.0.0.1:{port}/ws",
                headers={"Authorization": "Bearer token-a"},
            ) as ws_a, s.ws_connect(
                f"ws://127.0.0.1:{port}/ws",
                headers={"Authorization": "Bearer token-b"},
            ) as ws_b:
                # Drain the gateway.ready events
                await ws_a.receive_str()
                await ws_b.receive_str()

                created = await _rpc(ws_a, 1, "session.create")
                sid_a = created["result"]["session_id"]

                # B tries to close A's session — must report not-closed
                # because A's in-flight state is invisible to B.
                resp = await _rpc(
                    ws_b, 2, "session.close", {"session_id": sid_a}
                )
                assert resp["result"]["closed"] is False

                # A can still close its own session.
                resp = await _rpc(
                    ws_a, 3, "session.close", {"session_id": sid_a}
                )
                assert resp["result"]["closed"] is True
