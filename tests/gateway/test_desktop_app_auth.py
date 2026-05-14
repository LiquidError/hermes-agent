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

    def test_new_record_has_no_last_seen(self, tmp_path):
        from gateway.platforms.desktop_app_auth import TokenStore

        store = TokenStore(tmp_path / "tokens.json")
        store.add("client-a", "tok1")
        rec = store.list()[0]
        assert rec.last_seen_at is None

    def test_touch_records_timestamp(self, tmp_path):
        import time

        from gateway.platforms.desktop_app_auth import TokenStore

        store = TokenStore(tmp_path / "tokens.json")
        store.add("client-a", "tok1")

        before = time.time()
        store.touch("client-a")
        after = time.time()

        rec = store.list()[0]
        assert rec.last_seen_at is not None
        assert before <= rec.last_seen_at <= after

    def test_touch_unknown_client_is_noop(self, tmp_path):
        from gateway.platforms.desktop_app_auth import TokenStore

        store = TokenStore(tmp_path / "tokens.json")
        store.add("client-a", "tok1")
        # No exception, no mutation to other records.
        store.touch("not-a-client")
        rec = store.list()[0]
        assert rec.last_seen_at is None

    def test_last_seen_persists_across_reload(self, tmp_path):
        from gateway.platforms.desktop_app_auth import TokenStore

        path = tmp_path / "tokens.json"
        first = TokenStore(path)
        first.add("client-a", "tok1")
        first.touch("client-a")
        first.save()

        rec = TokenStore(path).list()[0]
        assert rec.last_seen_at is not None

    def test_loads_legacy_records_without_last_seen(self, tmp_path):
        # Token files written before this field existed must still load.
        import json

        path = tmp_path / "tokens.json"
        path.write_text(
            json.dumps([{"name": "client-a", "token_hash": "abc" * 22}])
        )

        from gateway.platforms.desktop_app_auth import TokenStore

        rec = TokenStore(path).list()[0]
        assert rec.name == "client-a"
        assert rec.last_seen_at is None

    def test_save_writes_file_with_owner_only_perms(self, tmp_path):
        """Token store is auth state — must not be group/other-readable.
        Inheriting the process umask leaves it 0o644 on most systems, which
        lets a co-tenant user read the hashes or replace the file.
        """
        import os
        import stat
        import sys

        if sys.platform == "win32":
            pytest.skip("POSIX file modes not meaningful on Windows")

        from gateway.platforms.desktop_app_auth import TokenStore

        path = tmp_path / "tokens.json"
        store = TokenStore(path)
        store.add("client-a", "tok1")
        # Force a permissive default umask so inherited perms would fail.
        prev = os.umask(0o000)
        try:
            store.save()
        finally:
            os.umask(prev)

        mode = stat.S_IMODE(path.stat().st_mode)
        # Owner read/write only — group/other bits all clear.
        assert mode & 0o077 == 0, f"token file mode 0o{mode:o} leaks to non-owner"

    def test_concurrent_verify_and_touch_does_not_race(self, tmp_path):
        """Two concurrent WS handler tasks call verify()/touch() on a shared
        store. Without the lock, verify()'s iteration could see _records
        mid-mutation; touch()'s last_seen_at write could be lost. The lock
        makes both atomic.
        """
        import threading

        from gateway.platforms.desktop_app_auth import TokenStore

        store = TokenStore(tmp_path / "tokens.json")
        for i in range(20):
            store.add(f"client-{i}", f"tok-{i}")

        errors: list[Exception] = []
        stop = threading.Event()

        def verifier():
            try:
                while not stop.is_set():
                    assert store.verify("tok-7") == "client-7"
            except Exception as exc:
                errors.append(exc)

        def toucher():
            try:
                while not stop.is_set():
                    store.touch("client-7")
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=verifier, daemon=True),
            threading.Thread(target=verifier, daemon=True),
            threading.Thread(target=toucher, daemon=True),
            threading.Thread(target=toucher, daemon=True),
        ]
        for t in threads:
            t.start()
        threading.Event().wait(0.4)
        stop.set()
        for t in threads:
            t.join(timeout=1.0)
        assert errors == [], f"concurrent access raised: {errors}"


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
    async def test_successful_auth_persists_last_seen(self, auth_adapter, tmp_path):
        # Reading the on-disk file directly verifies the touch was persisted,
        # not just held in the adapter's in-memory store.
        import json
        import time

        port = auth_adapter
        before = time.time()
        async with ClientSession() as s:
            async with s.ws_connect(
                f"ws://127.0.0.1:{port}/ws",
                headers={"Authorization": "Bearer valid-token"},
            ):
                pass
        after = time.time()

        on_disk = json.loads((tmp_path / "tokens.json").read_text())
        record = next(r for r in on_disk if r["name"] == "client-a")
        assert record["last_seen_at"] is not None
        assert before <= record["last_seen_at"] <= after

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
