"""Tests for the dashboard bind-config guard.

This file covers the pure-function guard in Task 2. The integration with
``start_server`` lands in Task 5 and tests live in the same file.
"""

import pytest

from hermes_cli.web_server import BindRefused, _validate_bind_config


def test_loopback_passes_without_tls_or_key():
    # Loopback bind is always allowed; no warnings.
    result = _validate_bind_config(host="127.0.0.1", has_tls=False, api_key="", allow_insecure=False)
    assert result.warnings == []


def test_non_loopback_without_tls_refused():
    with pytest.raises(BindRefused, match="TLS"):
        _validate_bind_config(host="0.0.0.0", has_tls=False, api_key="real-key-32-chars-XXXXXXXXXXXXX", allow_insecure=False)


def test_non_loopback_without_key_refused():
    with pytest.raises(BindRefused, match="API_SERVER_KEY"):
        _validate_bind_config(host="0.0.0.0", has_tls=True, api_key="", allow_insecure=False)


def test_non_loopback_with_placeholder_key_refused():
    with pytest.raises(BindRefused, match="placeholder"):
        _validate_bind_config(host="0.0.0.0", has_tls=True, api_key="changeme", allow_insecure=False)


def test_non_loopback_with_tls_and_key_passes():
    result = _validate_bind_config(
        host="0.0.0.0", has_tls=True, api_key="x" * 32, allow_insecure=False
    )
    assert result.warnings == []


def test_allow_insecure_passes_with_warning():
    result = _validate_bind_config(host="0.0.0.0", has_tls=False, api_key="", allow_insecure=True)
    assert any("insecure" in w.lower() for w in result.warnings)


def test_loopback_with_allow_insecure_emits_no_warning():
    """Loopback bind returns early; allow_insecure has no effect there."""
    result = _validate_bind_config(host="127.0.0.1", has_tls=False, api_key="", allow_insecure=True)
    assert result.warnings == []


def test_ipv6_loopback_passes():
    """IPv6 loopback (::1) is treated as loopback by is_network_accessible."""
    result = _validate_bind_config(host="::1", has_tls=False, api_key="", allow_insecure=False)
    assert result.warnings == []


# --- start_server integration ----------------------------------------

import os
from unittest.mock import patch

from hermes_cli import web_server


def test_start_server_loopback_runs(monkeypatch):
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    monkeypatch.delenv("HERMES_ALLOW_INSECURE_BIND", raising=False)
    with patch.object(web_server.uvicorn, "run") as run:
        web_server.start_server(host="127.0.0.1", port=9119, open_browser=False)
        run.assert_called_once()


def test_start_server_non_loopback_no_tls_no_escape_refuses(monkeypatch):
    monkeypatch.setenv("API_SERVER_KEY", "x" * 32)
    monkeypatch.delenv("HERMES_ALLOW_INSECURE_BIND", raising=False)
    with patch.object(web_server.uvicorn, "run") as run:
        with pytest.raises(BindRefused, match="TLS"):
            web_server.start_server(host="0.0.0.0", port=9119, open_browser=False)
        run.assert_not_called()


def test_start_server_non_loopback_no_key_refuses(monkeypatch, fake_cert_pair):
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    monkeypatch.delenv("HERMES_ALLOW_INSECURE_BIND", raising=False)
    # Pretend a cert exists by pointing at a tmp_path pair generated separately.
    cert_path, key_path = fake_cert_pair
    monkeypatch.setenv("HERMES_TLS_CERT", str(cert_path))
    monkeypatch.setenv("HERMES_TLS_KEY", str(key_path))
    with patch.object(web_server.uvicorn, "run") as run:
        with pytest.raises(BindRefused, match="API_SERVER_KEY"):
            web_server.start_server(host="0.0.0.0", port=9119, open_browser=False)
        run.assert_not_called()


def test_start_server_allow_insecure_runs_with_warning(monkeypatch, caplog):
    monkeypatch.setenv("HERMES_ALLOW_INSECURE_BIND", "1")
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    caplog.set_level("WARNING", logger="hermes_cli.web_server")
    with patch.object(web_server.uvicorn, "run") as run:
        web_server.start_server(host="0.0.0.0", port=9119, open_browser=False)
        run.assert_called_once()
    assert any("insecure" in r.message.lower() for r in caplog.records)


# --- _resolve_tls_paths --------------------------------------------------


def test_resolve_tls_paths_explicit_env(monkeypatch, tmp_path):
    cert = tmp_path / "explicit.crt"
    key = tmp_path / "explicit.key"
    monkeypatch.setenv("HERMES_TLS_CERT", str(cert))
    monkeypatch.setenv("HERMES_TLS_KEY", str(key))
    c, k = web_server._resolve_tls_paths(host="0.0.0.0")
    assert c == cert and k == key


def test_resolve_tls_paths_host_env(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_TLS_CERT", raising=False)
    monkeypatch.delenv("HERMES_TLS_KEY", raising=False)
    monkeypatch.setenv("HERMES_TLS_HOST", "myhost.ts.net")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    c, k = web_server._resolve_tls_paths(host="0.0.0.0")
    assert c == tmp_path / "tls" / "myhost.ts.net.crt"
    assert k == tmp_path / "tls" / "myhost.ts.net.key"


def test_resolve_tls_paths_glob_single_match(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_TLS_CERT", raising=False)
    monkeypatch.delenv("HERMES_TLS_KEY", raising=False)
    monkeypatch.delenv("HERMES_TLS_HOST", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    tls_dir = tmp_path / "tls"
    tls_dir.mkdir()
    (tls_dir / "found.ts.net.crt").write_text("c")
    (tls_dir / "found.ts.net.key").write_text("k")
    c, k = web_server._resolve_tls_paths(host="0.0.0.0")
    assert c == tls_dir / "found.ts.net.crt"
    assert k == tls_dir / "found.ts.net.key"


def test_resolve_tls_paths_glob_ambiguous_returns_none(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_TLS_CERT", raising=False)
    monkeypatch.delenv("HERMES_TLS_KEY", raising=False)
    monkeypatch.delenv("HERMES_TLS_HOST", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    tls_dir = tmp_path / "tls"
    tls_dir.mkdir()
    (tls_dir / "a.ts.net.crt").write_text("c")
    (tls_dir / "a.ts.net.key").write_text("k")
    (tls_dir / "b.ts.net.crt").write_text("c")
    (tls_dir / "b.ts.net.key").write_text("k")
    c, k = web_server._resolve_tls_paths(host="0.0.0.0")
    assert c is None and k is None


def test_resolve_tls_paths_none_when_no_files(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_TLS_CERT", raising=False)
    monkeypatch.delenv("HERMES_TLS_KEY", raising=False)
    monkeypatch.delenv("HERMES_TLS_HOST", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    c, k = web_server._resolve_tls_paths(host="0.0.0.0")
    assert c is None and k is None
