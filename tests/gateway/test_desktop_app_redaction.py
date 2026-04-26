"""Secret redaction and `config.reveal_secret` for remote clients.

`config.get` masks values whose keys match common secret patterns when
the calling dispatcher state is flagged remote (DesktopAppAdapter sets
this). The TUI's stdio path is unaffected. `config.reveal_secret`
returns the plaintext for a single key with rate-limiting.
"""

from __future__ import annotations

import json
import socket

import pytest
import pytest_asyncio
from aiohttp import ClientSession

from gateway.config import PlatformConfig
from gateway.platforms.desktop_app import DesktopAppAdapter
from gateway.platforms.desktop_app_auth import TokenStore


# ---------------------------------------------------------------------------
# Pattern + masking primitives
# ---------------------------------------------------------------------------


class TestSecretKeyDetection:
    def test_obvious_suffixes_are_secret(self):
        from tui_gateway.server import _is_secret_key

        for k in (
            "openai_api_key",
            "TELEGRAM_BOT_TOKEN",
            "ANTHROPIC_API_KEY",
            "client_secret",
            "github_password",
            "user_credentials",
        ):
            assert _is_secret_key(k), f"{k!r} should match"

    def test_benign_keys_are_not_secret(self):
        from tui_gateway.server import _is_secret_key

        for k in ("model", "host", "port", "personality", "skin", "tools"):
            assert not _is_secret_key(k), f"{k!r} should not match"


class TestMaskValue:
    def test_short_value_becomes_stars(self):
        from tui_gateway.server import _mask_value

        assert _mask_value("abc") == "***"

    def test_long_value_keeps_prefix_and_suffix(self):
        from tui_gateway.server import _mask_value

        masked = _mask_value("sk-1234567890abcdef")
        assert masked.startswith("sk-1")
        assert masked.endswith("cdef")
        assert "..." in masked
        # The middle is hidden — the whole token must NOT appear.
        assert "1234567890" not in masked

    def test_non_string_passes_through(self):
        from tui_gateway.server import _mask_value

        assert _mask_value(42) == 42
        assert _mask_value(True) is True
        assert _mask_value(None) is None


class TestRedactDict:
    def test_redacts_top_level_secret_key(self):
        from tui_gateway.server import _redact_dict

        out = _redact_dict({"openai_api_key": "sk-1234567890ab", "model": "gpt-4"})
        assert "..." in out["openai_api_key"]
        assert out["model"] == "gpt-4"

    def test_redacts_nested_secret(self):
        from tui_gateway.server import _redact_dict

        out = _redact_dict(
            {"providers": {"openrouter": {"api_key": "sk-secretvalue123"}}}
        )
        assert "..." in out["providers"]["openrouter"]["api_key"]


# ---------------------------------------------------------------------------
# `config.get key="full"` honours the per-state redact_secrets flag.
# ---------------------------------------------------------------------------


class TestConfigGetRedaction:
    def test_default_state_returns_unredacted(self, monkeypatch):
        from tui_gateway import server

        cfg = {"openai_api_key": "sk-1234567890ab", "model": "gpt-4"}
        monkeypatch.setattr(server, "_load_cfg", lambda: cfg)

        handler = server._methods["config.get"]
        resp = handler(1, {"key": "full"})

        assert resp["result"]["config"]["openai_api_key"] == "sk-1234567890ab"

    def test_remote_state_redacts(self, monkeypatch):
        from tui_gateway import server

        cfg = {"openai_api_key": "sk-1234567890ab", "model": "gpt-4"}
        monkeypatch.setattr(server, "_load_cfg", lambda: cfg)

        bound = server._DispatcherState()
        bound.redact_secrets = True
        token = server._state_var.set(bound)
        try:
            handler = server._methods["config.get"]
            resp = handler(1, {"key": "full"})
            assert "..." in resp["result"]["config"]["openai_api_key"]
            assert resp["result"]["config"]["model"] == "gpt-4"
        finally:
            server._state_var.reset(token)


# ---------------------------------------------------------------------------
# `config.reveal_secret` returns plaintext, rate-limited.
# ---------------------------------------------------------------------------


class TestConfigRevealSecret:
    def test_returns_plaintext_value(self, monkeypatch):
        from tui_gateway import server

        cfg = {"openai_api_key": "sk-1234567890ab"}
        monkeypatch.setattr(server, "_load_cfg", lambda: cfg)
        monkeypatch.setattr(server, "_reveal_timestamps", [])

        handler = server._methods["config.reveal_secret"]
        resp = handler(1, {"key": "openai_api_key"})

        assert resp["result"]["value"] == "sk-1234567890ab"

    def test_unknown_key_returns_error(self, monkeypatch):
        from tui_gateway import server

        monkeypatch.setattr(server, "_load_cfg", lambda: {})
        monkeypatch.setattr(server, "_reveal_timestamps", [])

        handler = server._methods["config.reveal_secret"]
        resp = handler(1, {"key": "nonexistent_key"})

        assert "error" in resp

    def test_rate_limit_returns_429_after_window(self, monkeypatch):
        from tui_gateway import server

        cfg = {"openai_api_key": "sk-1234567890ab"}
        monkeypatch.setattr(server, "_load_cfg", lambda: cfg)
        monkeypatch.setattr(server, "_reveal_timestamps", [])
        monkeypatch.setattr(server, "_REVEAL_MAX_PER_WINDOW", 3)

        handler = server._methods["config.reveal_secret"]

        for i in range(3):
            resp = handler(i, {"key": "openai_api_key"})
            assert "result" in resp, f"call {i} should succeed"

        resp = handler(99, {"key": "openai_api_key"})
        assert "error" in resp
        assert resp["error"]["code"] == 429


# ---------------------------------------------------------------------------
# End-to-end: DesktopAppAdapter delivers redacted config over the WS.
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest_asyncio.fixture
async def redaction_adapter(tmp_path, monkeypatch):
    from tui_gateway import server as tg

    monkeypatch.setattr(
        tg, "_load_cfg", lambda: {"openai_api_key": "sk-1234567890ab", "model": "gpt-4"}
    )

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
    assert await adapter.connect()
    try:
        yield port
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_full_config_returned_via_ws_is_redacted(redaction_adapter):
    port = redaction_adapter
    async with ClientSession() as s:
        async with s.ws_connect(
            f"ws://127.0.0.1:{port}/ws",
            headers={"Authorization": "Bearer valid-token"},
        ) as ws:
            await ws.receive_str()  # gateway.ready

            await ws.send_json(
                {"jsonrpc": "2.0", "id": 1, "method": "config.get", "params": {"key": "full"}}
            )
            while True:
                msg = json.loads(await ws.receive_str())
                if msg.get("id") == 1:
                    break

            cfg = msg["result"]["config"]
            assert "..." in cfg["openai_api_key"]
            assert cfg["model"] == "gpt-4"
