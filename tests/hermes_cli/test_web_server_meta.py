"""Tests for /api/meta capability probe."""

import pytest
from fastapi.testclient import TestClient

from hermes_cli import web_server


@pytest.fixture
def auth_headers():
    return {web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN}


@pytest.fixture
def client():
    web_server.app.state.bound_host = "127.0.0.1"
    c = TestClient(web_server.app)
    c.headers["host"] = "127.0.0.1"
    yield c
    web_server.app.state.bound_host = None


def test_meta_requires_auth(client):
    r = client.get("/api/meta")
    assert r.status_code == 401


def test_meta_returns_full_shape(client, auth_headers):
    r = client.get("/api/meta", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert "hermes_version" in body
    assert isinstance(body["hermes_version"], str)
    assert "services" in body and isinstance(body["services"], dict)
    assert "gateway" in body["services"]
    assert "dashboard" in body["services"]
    assert "available" in body["services"]["dashboard"]
    assert "endpoints" in body["services"]["dashboard"]
    assert isinstance(body["services"]["dashboard"]["endpoints"], list)
    assert "plugins" in body and isinstance(body["plugins"], list)
    assert "agent_profiles" in body and isinstance(body["agent_profiles"], list)
    assert "active_profile" in body


def test_meta_dashboard_endpoints_listed(client, auth_headers):
    r = client.get("/api/meta", headers=auth_headers)
    eps = r.json()["services"]["dashboard"]["endpoints"]
    # Spot-check a handful that we know exist.
    assert "/api/status" in eps
    assert "/api/sessions" in eps
    assert "/api/config/schema" in eps
    # Plugin paths must NOT appear here — they live under plugins[].
    assert not any(p.startswith("/api/plugins/") for p in eps)


def test_meta_plugins_have_name_enabled_prefix(client, auth_headers):
    r = client.get("/api/meta", headers=auth_headers)
    plugins = r.json()["plugins"]
    for p in plugins:
        assert "name" in p
        assert "enabled" in p
        assert "prefix" in p
        assert p["prefix"].startswith("/api/plugins/")


def test_meta_omits_tls_when_no_cert(client, auth_headers, monkeypatch):
    monkeypatch.setattr(web_server, "_TLS_CONTEXT", None, raising=False)
    r = client.get("/api/meta", headers=auth_headers)
    body = r.json()
    assert "tls" not in body


def test_meta_includes_tls_when_cert_loaded(client, auth_headers, monkeypatch, tmp_path):
    from datetime import datetime, timedelta, timezone
    from hermes_cli.tls_loader import TLSContext
    fake = TLSContext(
        cert_path=tmp_path / "x.crt",
        key_path=tmp_path / "x.key",
        not_after=datetime.now(timezone.utc) + timedelta(days=60),
        expires_soon=False,
    )
    monkeypatch.setattr(web_server, "_TLS_CONTEXT", fake, raising=False)
    r = client.get("/api/meta", headers=auth_headers)
    body = r.json()
    assert "tls" in body
    assert body["tls"]["expires_soon"] is False
    assert body["tls"]["not_after"].startswith("20")  # ISO-ish


def test_meta_reports_local_gateway_running_without_health_url(
    client, auth_headers, monkeypatch
):
    """Default same-host deployment: GATEWAY_HEALTH_URL is unset, but a local
    gateway PID exists. /api/meta must report the gateway as available.
    Previously it always returned available=false in this configuration
    because _cached_gateway_health() short-circuits when the URL is unset.
    """
    monkeypatch.setattr(web_server, "_GATEWAY_HEALTH_URL", None, raising=False)
    monkeypatch.setattr(web_server, "get_running_pid", lambda: 4242)
    r = client.get("/api/meta", headers=auth_headers)
    body = r.json()
    assert body["services"]["gateway"]["available"] is True
    assert body["services"]["gateway"]["url"] is None


def test_meta_falls_back_to_remote_probe_when_no_local_pid(
    client, auth_headers, monkeypatch
):
    """Containerised deployment: no local PID, but the operator pointed
    GATEWAY_HEALTH_URL at the gateway pod. /api/meta must consult the
    cached probe and reflect its result.
    """
    monkeypatch.setattr(
        web_server, "_GATEWAY_HEALTH_URL", "http://gateway:8642", raising=False
    )
    monkeypatch.setattr(web_server, "get_running_pid", lambda: None)

    async def _fake_probe():
        return True, {"pid": 99}

    monkeypatch.setattr(web_server, "_cached_gateway_health", _fake_probe)
    r = client.get("/api/meta", headers=auth_headers)
    body = r.json()
    assert body["services"]["gateway"]["available"] is True
    assert body["services"]["gateway"]["url"] == "http://gateway:8642"


def test_meta_reports_gateway_unavailable_when_neither_works(
    client, auth_headers, monkeypatch
):
    monkeypatch.setattr(web_server, "_GATEWAY_HEALTH_URL", None, raising=False)
    monkeypatch.setattr(web_server, "get_running_pid", lambda: None)
    r = client.get("/api/meta", headers=auth_headers)
    body = r.json()
    assert body["services"]["gateway"]["available"] is False
