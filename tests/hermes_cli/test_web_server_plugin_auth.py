"""Plugin route auth uniformity tests."""

import pytest
from fastapi.testclient import TestClient

from hermes_cli import web_server


@pytest.fixture
def client():
    web_server.app.state.bound_host = "127.0.0.1"
    c = TestClient(web_server.app)
    c.headers["host"] = "127.0.0.1"
    yield c


@pytest.fixture
def auth_headers():
    return {web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN}


def _has_route(prefix: str) -> bool:
    return any(getattr(r, "path", "").startswith(prefix) for r in web_server.app.routes)


# The example plugin lives under plugins/example-dashboard/ but its manifest
# declares ``name: example``, so its router mounts at /api/plugins/example/.
# We probe that real route (not /api/plugins/example-dashboard/) for the
# auth-uniformity check.
@pytest.mark.skipif(
    not _has_route("/api/plugins/example"),
    reason="example plugin not mounted in this environment",
)
def test_plugin_route_rejects_missing_token(client):
    r = client.get("/api/plugins/example/hello")
    assert r.status_code == 401


@pytest.mark.skipif(
    not _has_route("/api/plugins/example"),
    reason="example plugin not mounted in this environment",
)
def test_plugin_route_accepts_valid_token(client, auth_headers):
    r = client.get("/api/plugins/example/hello", headers=auth_headers)
    assert r.status_code == 200
