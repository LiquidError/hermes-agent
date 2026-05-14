"""Fixtures shared across hermes_cli kanban tests."""

from __future__ import annotations

import pytest


@pytest.fixture
def all_assignees_spawnable(monkeypatch):
    """Pretend every assignee maps to a real Hermes profile.

    Most dispatcher tests use synthetic assignees ("alice", "bob") that
    don't correspond to actual profile directories on disk. Without this
    patch, the dispatcher's profile-exists guard (PR #20105) routes
    those tasks into ``skipped_nonspawnable`` instead of spawning, which
    would break tests that assert spawn behavior.
    """
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)


@pytest.fixture(autouse=True)
def _reset_dashboard_state():
    """Reset web_server module/app state after each test.

    Task 5+'s start_server() sets app.state.bound_host, app.state.bound_port,
    and web_server._TLS_CONTEXT at listen time. Tests that mock uvicorn.run
    leave those values set, which then leak into later tests on the same
    xdist worker — observable as path-traversal tests returning 400 instead
    of 200/404, or /api/meta surfacing TLS info from a previous test's cert.
    """
    yield
    try:
        from hermes_cli import web_server
        web_server.app.state.bound_host = None
        web_server.app.state.bound_port = None
        web_server.app.state.allow_insecure = False
        web_server._TLS_CONTEXT = None
    except Exception:
        pass
