"""Tests verifying start_server passes ssl_keyfile/ssl_certfile to uvicorn."""

from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_cli import web_server


def test_uvicorn_called_with_ssl_when_cert_present(monkeypatch, fake_cert_pair):
    cert_path, key_path = fake_cert_pair
    monkeypatch.setenv("HERMES_TLS_CERT", str(cert_path))
    monkeypatch.setenv("HERMES_TLS_KEY", str(key_path))
    monkeypatch.setenv("API_SERVER_KEY", "x" * 32)
    with patch.object(web_server.uvicorn, "run") as run:
        web_server.start_server(host="0.0.0.0", port=9119, open_browser=False)
    args, kwargs = run.call_args
    assert kwargs.get("ssl_keyfile") == str(key_path)
    assert kwargs.get("ssl_certfile") == str(cert_path)


def test_uvicorn_called_without_ssl_when_no_cert(monkeypatch):
    monkeypatch.delenv("HERMES_TLS_CERT", raising=False)
    monkeypatch.delenv("HERMES_TLS_KEY", raising=False)
    monkeypatch.delenv("HERMES_TLS_HOST", raising=False)
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    with patch.object(web_server.uvicorn, "run") as run:
        web_server.start_server(host="127.0.0.1", port=9119, open_browser=False)
    _, kwargs = run.call_args
    assert kwargs.get("ssl_keyfile") is None
    assert kwargs.get("ssl_certfile") is None
