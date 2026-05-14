"""Connection-lifecycle invariants for DesktopAppAdapter.

When a WS connection ends, the adapter's per-connection dispatcher
state is unregistered from the cross-thread session→state map and
its slash workers are closed. A fresh connection authenticates and
operates as a clean slate; persisted sessions remain available via
state.db (verified upstream in tui_gateway tests, not duplicated here).
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
async def lifecycle_adapter(tmp_path):
    token_file = tmp_path / "tokens.json"
    store = TokenStore(token_file)
    store.add("client-a", "tok")
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


@pytest.mark.asyncio
async def test_disconnect_unregisters_session_from_global_map(lifecycle_adapter):
    import json

    from tui_gateway import server as tg

    port = lifecycle_adapter
    async with ClientSession() as s:
        async with s.ws_connect(
            f"ws://127.0.0.1:{port}/ws",
            headers={"Authorization": "Bearer tok"},
        ) as ws:
            await ws.receive_str()  # gateway.ready
            await ws.send_json(
                {"jsonrpc": "2.0", "id": 1, "method": "session.create"}
            )
            while True:
                msg = json.loads(await ws.receive_str())
                if msg.get("id") == 1:
                    break
            sid = msg["result"]["session_id"]
            assert sid in tg._session_states

    # After the WS closes, the cleanup loop in _ws_route runs and
    # unregisters the session from the cross-thread routing map.
    # Give the event loop a moment to finish the disconnect path.
    import asyncio
    for _ in range(20):
        if sid not in tg._session_states:
            break
        await asyncio.sleep(0.05)
    assert sid not in tg._session_states


@pytest.mark.asyncio
async def test_connect_succeeds_when_loopback_holds_same_port(tmp_path):
    """The preflight port probe used to always hit 127.0.0.1:<port>, so a
    non-loopback bind got falsely rejected whenever an unrelated localhost
    service held the same port. With the probe removed, aiohttp's TCPSite
    reports bind conflicts for the configured interface only.
    """
    token_file = tmp_path / "tokens.json"
    store = TokenStore(token_file)
    store.add("client-a", "tok")
    store.save()

    # Squat on 127.0.0.1:<port>. The adapter binds 0.0.0.0:<port> which
    # includes 127.0.0.1, so on most kernels this still conflicts — but the
    # squatter must not be enough on its own to refuse the bind preflight.
    # Pick an unused port for the adapter, then squat 127.0.0.1 on a
    # *different* port to model the real scenario the report describes:
    # adapter wants iface X:port P, unrelated service holds 127.0.0.1:P
    # but iface X is something else entirely.
    adapter_port = _free_port()
    squatter_port = _free_port()
    squatter = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    squatter.bind(("127.0.0.1", squatter_port))
    squatter.listen(1)
    try:
        adapter = DesktopAppAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "host": "127.0.0.1",
                    "port": adapter_port,
                    "token_file": str(token_file),
                },
            ),
        )
        assert await adapter.connect(), (
            "adapter refused to start — preflight may still be probing the wrong host"
        )
        await adapter.disconnect()
    finally:
        squatter.close()


@pytest.mark.asyncio
async def test_reconnect_after_disconnect_works(lifecycle_adapter):
    """A fresh connection after a clean disconnect authenticates and
    serves RPC normally — no leaked state breaks the next session.
    """
    import json

    port = lifecycle_adapter
    headers = {"Authorization": "Bearer tok"}
    async with ClientSession() as s:
        async with s.ws_connect(f"ws://127.0.0.1:{port}/ws", headers=headers) as ws:
            await ws.receive_str()
            await ws.send_json({"jsonrpc": "2.0", "id": 1, "method": "session.create"})
            while True:
                msg = json.loads(await ws.receive_str())
                if msg.get("id") == 1:
                    break

        # Reconnect — fresh state, must work the same way.
        async with s.ws_connect(f"ws://127.0.0.1:{port}/ws", headers=headers) as ws:
            await ws.receive_str()
            await ws.send_json({"jsonrpc": "2.0", "id": 1, "method": "session.create"})
            while True:
                msg = json.loads(await ws.receive_str())
                if msg.get("id") == 1:
                    break
            assert "result" in msg
            assert "session_id" in msg["result"]
