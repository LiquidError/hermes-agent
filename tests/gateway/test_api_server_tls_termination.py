"""Verify api_server constructs TCPSite with ssl_context when cert files exist.

Task 9 of the dashboard fork: the gateway shares ``hermes_cli.tls_loader``
with the dashboard. When ``HERMES_TLS_CERT``/``HERMES_TLS_KEY`` are set,
``APIServerAdapter.connect()`` must wire an ``ssl.SSLContext`` into
``aiohttp.web.TCPSite``. Without those env vars, ``ssl_context`` stays
``None`` and the gateway runs plaintext (current behavior preserved).
"""

import secrets
import ssl
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


@pytest.mark.asyncio
async def test_tcpsite_gets_ssl_context_when_tls_files_present(
    monkeypatch, fake_cert_pair
):
    """When HERMES_TLS_CERT/HERMES_TLS_KEY are set, the gateway constructs
    TCPSite with an ssl.SSLContext loaded from the cert pair."""
    cert_path, key_path = fake_cert_pair
    monkeypatch.setenv("HERMES_TLS_CERT", str(cert_path))
    monkeypatch.setenv("HERMES_TLS_KEY", str(key_path))

    captured: dict = {}

    def _stub_tcpsite(runner, host, port, *, ssl_context=None, **kw):
        captured["ssl_context"] = ssl_context
        captured["host"] = host
        captured["port"] = port
        site = AsyncMock()
        site.start = AsyncMock(return_value=None)
        return site

    runner_stub = SimpleNamespace(setup=AsyncMock(return_value=None))
    monkeypatch.setattr(
        "gateway.platforms.api_server.web.AppRunner", lambda app: runner_stub
    )
    monkeypatch.setattr(
        "gateway.platforms.api_server.web.TCPSite", _stub_tcpsite
    )

    api_key = secrets.token_hex(16)  # 32-char hex, high entropy
    adapter = APIServerAdapter(
        PlatformConfig(
            enabled=True,
            extra={"host": "0.0.0.0", "port": 8642, "key": api_key},
        )
    )
    started = await adapter.connect()
    assert started is True, "adapter.connect() should return True after TLS setup"
    assert isinstance(captured["ssl_context"], ssl.SSLContext)


@pytest.mark.asyncio
async def test_tcpsite_no_ssl_context_when_env_unset(monkeypatch):
    """Plaintext path is preserved when HERMES_TLS_* env vars are absent."""
    monkeypatch.delenv("HERMES_TLS_CERT", raising=False)
    monkeypatch.delenv("HERMES_TLS_KEY", raising=False)

    captured: dict = {}

    def _stub_tcpsite(runner, host, port, *, ssl_context=None, **kw):
        captured["ssl_context"] = ssl_context
        site = AsyncMock()
        site.start = AsyncMock(return_value=None)
        return site

    runner_stub = SimpleNamespace(setup=AsyncMock(return_value=None))
    monkeypatch.setattr(
        "gateway.platforms.api_server.web.AppRunner", lambda app: runner_stub
    )
    monkeypatch.setattr(
        "gateway.platforms.api_server.web.TCPSite", _stub_tcpsite
    )

    adapter = APIServerAdapter(
        PlatformConfig(
            enabled=True,
            extra={"host": "127.0.0.1", "port": 8642},
        )
    )
    started = await adapter.connect()
    assert started is True
    assert captured["ssl_context"] is None
