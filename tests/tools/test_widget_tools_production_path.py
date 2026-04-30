"""End-to-end production path: model_tools.handle_function_call → registry.dispatch
must reach the widget handler with the right session context bound.

The handler reads the session_key from gateway.session_context's
HERMES_SESSION_KEY contextvar (or, in tests, from an explicit
session_id kwarg). Production callers do NOT pass session_id to
registry.dispatch — only task_id. Without the contextvar fallback the
handler reports 'session not found' for every call.
"""

from __future__ import annotations

import json
import threading
import types

import pytest

from tools import widget_tools  # noqa: F401  triggers registration
from tui_gateway import server, widget_runtime


@pytest.fixture
def session(monkeypatch):
    sid, key = "sess-prod", "key-prod"
    transport = types.SimpleNamespace(write=lambda *a, **k: True)
    state = server._state()
    state.sessions[sid] = {
        "session_key": key,
        "transport": transport,
        "client_capabilities": ["widget.render"],
        "widget_registry": widget_runtime.WidgetRegistry(),
        "api_call_registry": widget_runtime.ApiCallRegistry(),
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
    }
    server._register_session(sid)
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    yield sid, key, state.sessions[sid]
    state.sessions.pop(sid, None)
    server._unregister_session(sid)


def test_handle_function_call_dispatches_widget_dispose_with_contextvar_session(session, monkeypatch):
    """Production path: HERMES_SESSION_KEY is bound, no session_id kwarg, tool resolves session.

    handle_function_call passes only task_id to registry.dispatch — the widget
    handler must fall back to the session_key contextvar. The handler is the
    real production path; if this test fails, every widget tool call from
    AIAgent returns 'session not found'.
    """
    import model_tools
    from gateway.session_context import set_session_vars, clear_session_vars

    sid, key, sess = session
    cid = sess["widget_registry"].allocate(
        source="x", capabilities=[], title=None, initial_size=None, trace_id=None,
    )

    tokens = set_session_vars(session_key=key)
    try:
        result = model_tools.handle_function_call(
            "widget_dispose",
            {"card_id": cid, "reason": "task_complete"},
            task_id="some-uuid-not-the-session-key",  # mimics production
        )
    finally:
        clear_session_vars(tokens)

    payload = json.loads(result)
    assert "session not found" not in result, (
        f"production path failed to resolve session: {result!r}"
    )
    assert payload.get("disposed") is True
    assert payload.get("already_disposed") is False


def test_handle_function_call_widget_update_via_production_path(session, monkeypatch):
    import model_tools
    from gateway.session_context import set_session_vars, clear_session_vars

    sid, key, sess = session
    cid = sess["widget_registry"].allocate(
        source="old", capabilities=["hermes.ask"], title=None, initial_size=None, trace_id=None,
    )

    tokens = set_session_vars(session_key=key)
    try:
        result = model_tools.handle_function_call(
            "widget_update",
            {"card_id": cid, "source": "new"},
            task_id="per-turn-uuid",
        )
    finally:
        clear_session_vars(tokens)

    payload = json.loads(result)
    assert payload == {"updated": True, "card_gone": False}
    assert sess["widget_registry"].get(cid).source == "new"


def test_handle_function_call_returns_error_when_no_session_context(session, monkeypatch):
    """When neither contextvar nor session_id kwarg is set, the tool reports
    a clean 'session not found' rather than crashing or hanging."""
    import model_tools

    result = model_tools.handle_function_call(
        "widget_dispose",
        {"card_id": "wgt_000000"},
        task_id="orphan-task",
    )
    payload = json.loads(result)
    assert payload.get("error", {}).get("code") == 4001
