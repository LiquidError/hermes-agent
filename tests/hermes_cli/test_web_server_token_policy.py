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


# --- Public-path exemption is loopback-only --------------------------

def test_loopback_public_paths_no_token(client):
    """On loopback, the documented public paths (e.g. /api/status) are
    reachable without a bearer — the SPA bootstrap path."""
    web_server.app.state.bound_host = "127.0.0.1"
    r = client.get("/api/status")
    assert r.status_code != 401


def test_off_loopback_public_paths_require_token(client, monkeypatch):
    """Off-loopback closes the public-path exemption: every /api/* requires
    the bearer. Otherwise config schema / model info / plugin list / rescan
    leak to anyone who can route to the dashboard."""
    web_server.app.state.bound_host = "0.0.0.0"
    monkeypatch.setenv("API_SERVER_KEY", "x" * 32)
    for path in (
        "/api/status",
        "/api/config/schema",
        "/api/model/info",
        "/api/dashboard/plugins",
    ):
        r = client.get(path)
        assert r.status_code == 401, f"{path} should be 401 off-loopback without bearer"


def test_off_loopback_public_paths_with_bearer_pass(client, monkeypatch):
    """Off-loopback with the API key, the previously-public paths work normally."""
    web_server.app.state.bound_host = "0.0.0.0"
    api_key = "x" * 32
    monkeypatch.setenv("API_SERVER_KEY", api_key)
    r = client.get("/api/status", headers={"Authorization": f"Bearer {api_key}"})
    assert r.status_code != 401


# --- Off-loopback strict mode: gate the entire path space ------------

def test_off_loopback_docs_require_bearer(client, monkeypatch):
    """OpenAPI /docs leaks every endpoint + schema — must require bearer
    off-loopback even though it's not under /api/*."""
    web_server.app.state.bound_host = "0.0.0.0"
    monkeypatch.setenv("API_SERVER_KEY", "x" * 32)
    for path in ("/docs", "/redoc", "/openapi.json"):
        r = client.get(path)
        assert r.status_code == 401, f"{path} should be 401 off-loopback without bearer"


def test_off_loopback_root_requires_bearer(client, monkeypatch):
    """SPA HTML at / embeds the ephemeral token and shouldn't render
    off-loopback to anyone without a bearer."""
    web_server.app.state.bound_host = "0.0.0.0"
    monkeypatch.setenv("API_SERVER_KEY", "x" * 32)
    r = client.get("/")
    assert r.status_code == 401


def test_off_loopback_arbitrary_path_requires_bearer(client, monkeypatch):
    """Static-asset-style path is gated too. Reduces fingerprinting +
    keeps the unauthenticated attack surface zero."""
    web_server.app.state.bound_host = "0.0.0.0"
    monkeypatch.setenv("API_SERVER_KEY", "x" * 32)
    r = client.get("/assets/random-bundle.js")
    assert r.status_code == 401


def test_off_loopback_docs_with_bearer_pass(client, monkeypatch):
    """With a valid bearer, /docs works off-loopback (operator can still
    inspect the API surface when authenticated)."""
    web_server.app.state.bound_host = "0.0.0.0"
    api_key = "x" * 32
    monkeypatch.setenv("API_SERVER_KEY", api_key)
    r = client.get("/docs", headers={"Authorization": f"Bearer {api_key}"})
    assert r.status_code != 401


def test_loopback_docs_unauthenticated_unchanged(client):
    """Loopback behaviour is unchanged: /docs is reachable without a
    bearer (the SPA + the FastAPI dev surface both work locally)."""
    web_server.app.state.bound_host = "127.0.0.1"
    r = client.get("/docs")
    assert r.status_code != 401
