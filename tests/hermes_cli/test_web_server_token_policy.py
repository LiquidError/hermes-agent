"""Tests for the bind-aware token policy in hermes_cli.web_server."""

import os

import pytest
from fastapi.testclient import TestClient

from hermes_cli import web_server


@pytest.fixture
def client(monkeypatch):
    """TestClient with bound_host configurable per-test."""
    web_server.app.state.bound_host = "127.0.0.1"
    c = TestClient(web_server.app)
    # Host-header middleware rejects TestClient's default "testserver" when
    # bound_host is set. Match the bound interface so requests pass through
    # to the auth middleware under test.
    c.headers["host"] = "127.0.0.1"
    yield c
    web_server.app.state.bound_host = None


# --- Loopback bind ----------------------------------------------------

def test_loopback_accepts_ephemeral_session_token(client):
    web_server.app.state.bound_host = "127.0.0.1"
    r = client.get(
        "/api/sessions",
        headers={web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN},
    )
    assert r.status_code != 401


def test_loopback_accepts_api_server_key_when_set(client, monkeypatch):
    web_server.app.state.bound_host = "127.0.0.1"
    monkeypatch.setenv("API_SERVER_KEY", "x" * 32)
    r = client.get(
        "/api/sessions",
        headers={"Authorization": "Bearer " + ("x" * 32)},
    )
    assert r.status_code != 401


def test_loopback_rejects_wrong_token(client):
    web_server.app.state.bound_host = "127.0.0.1"
    r = client.get(
        "/api/sessions",
        headers={"Authorization": "Bearer wrong-token-here"},
    )
    assert r.status_code == 401


# --- Non-loopback bind ------------------------------------------------

def test_non_loopback_rejects_ephemeral_session_token(client, monkeypatch):
    web_server.app.state.bound_host = "0.0.0.0"
    monkeypatch.setenv("API_SERVER_KEY", "x" * 32)
    r = client.get(
        "/api/sessions",
        headers={web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN},
    )
    assert r.status_code == 401


def test_non_loopback_accepts_api_server_key(client, monkeypatch):
    web_server.app.state.bound_host = "0.0.0.0"
    monkeypatch.setenv("API_SERVER_KEY", "x" * 32)
    r = client.get(
        "/api/sessions",
        headers={"Authorization": "Bearer " + ("x" * 32)},
    )
    assert r.status_code != 401


def test_non_loopback_missing_token_returns_401(client, monkeypatch):
    web_server.app.state.bound_host = "0.0.0.0"
    monkeypatch.setenv("API_SERVER_KEY", "x" * 32)
    r = client.get("/api/sessions")
    assert r.status_code == 401


def test_401_never_403_or_404(client, monkeypatch):
    web_server.app.state.bound_host = "0.0.0.0"
    monkeypatch.setenv("API_SERVER_KEY", "x" * 32)
    for header in [
        {},
        {"Authorization": "Bearer "},
        {"Authorization": "garbled"},
        {web_server._SESSION_HEADER_NAME: "wrong"},
    ]:
        r = client.get("/api/sessions", headers=header)
        assert r.status_code == 401, f"got {r.status_code} for {header}"


def test_bound_host_unset_defaults_to_loopback(client):
    """When app.state.bound_host is None, treat as loopback (test default)."""
    web_server.app.state.bound_host = None
    r = client.get(
        "/api/sessions",
        headers={
            web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN,
            "host": "127.0.0.1",
        },
    )
    assert r.status_code != 401


def test_loopback_accepts_session_token_via_bearer(client, monkeypatch):
    """Loopback dual-header path: ephemeral token also works in Bearer header."""
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    web_server.app.state.bound_host = "127.0.0.1"
    r = client.get(
        "/api/sessions",
        headers={"Authorization": f"Bearer {web_server._SESSION_TOKEN}"},
    )
    assert r.status_code != 401
