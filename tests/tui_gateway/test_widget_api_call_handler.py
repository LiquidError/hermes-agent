"""widget.api_call: synchronous validation + ack; spawns work in background."""

from __future__ import annotations

import threading
import types

import pytest

from tui_gateway import server, widget_runtime


def _seed_session_with_card(sid="sess-api", key="key-api", capabilities=None):
    state = server._state()
    transport = types.SimpleNamespace(write=lambda *a, **k: True)
    sess = {
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
    state.sessions[sid] = sess
    server._register_session(sid)
    cid = sess["widget_registry"].allocate(
        source="x", capabilities=capabilities or ["hermes.ask"],
        title=None, initial_size=None, trace_id=None,
    )
    return sid, key, sess, cid


def test_acks_synchronously_for_valid_call(monkeypatch):
    sid, key, sess, cid = _seed_session_with_card(sid="sess-ack", key="key-ack")
    # Stub the worker so the test doesn't actually run prompt.btw.
    monkeypatch.setattr(
        "tui_gateway.server._spawn_widget_api_call_worker",
        lambda **kwargs: None,
    )

    handler = server._methods["widget.api_call"]
    resp = handler(17, {
        "session_id": sid,
        "card_id": cid,
        "correlation_id": "corr_a1b2c3",
        "capability": "hermes.ask",
        "args": {"prompt": "What's Q3 revenue?"},
    })
    assert resp == {
        "jsonrpc": "2.0",
        "id": 17,
        "result": {"accepted": True, "correlation_id": "corr_a1b2c3"},
    }
    # Correlation registered in the per-session registry.
    assert sess["api_call_registry"].get("corr_a1b2c3") is not None


def test_rejects_unknown_session(monkeypatch):
    handler = server._methods["widget.api_call"]
    resp = handler(1, {
        "session_id": "ghost",
        "card_id": "wgt_000000",
        "correlation_id": "corr_x",
        "capability": "hermes.ask",
        "args": {},
    })
    assert "error" in resp
    assert resp["error"]["code"] == 4001  # session not found, base contract


def test_rejects_unknown_card(monkeypatch):
    sid, key, sess, _ = _seed_session_with_card(sid="sess-uc", key="key-uc")
    handler = server._methods["widget.api_call"]
    resp = handler(1, {
        "session_id": sid,
        "card_id": "wgt_deadbe",
        "correlation_id": "corr_x",
        "capability": "hermes.ask",
        "args": {},
    })
    assert resp["error"]["code"] == 4103


def test_rejects_capability_not_declared(monkeypatch):
    sid, key, sess, cid = _seed_session_with_card(
        sid="sess-undecl", key="key-undecl",
        capabilities=["notes.save"],  # hermes.ask not declared
    )
    handler = server._methods["widget.api_call"]
    resp = handler(1, {
        "session_id": sid,
        "card_id": cid,
        "correlation_id": "corr_x",
        "capability": "hermes.ask",
        "args": {},
    })
    assert resp["error"]["code"] == 4104


def test_rejects_unsupported_capability(monkeypatch):
    sid, key, sess, cid = _seed_session_with_card(sid="sess-bad", key="key-bad", capabilities=["bogus.thing"])
    # bogus.thing is not in the canvasAPI surface (Tauri broker would have
    # rejected client-side). Server-side guard catches the case where it
    # somehow reaches us.
    handler = server._methods["widget.api_call"]
    resp = handler(1, {
        "session_id": sid,
        "card_id": cid,
        "correlation_id": "corr_x",
        "capability": "bogus.thing",
        "args": {},
    })
    # Either 4101 (unknown capability) or 4104 (not declared) is acceptable;
    # both are correct rejections. Use 4101 since the cap doesn't exist at all.
    assert resp["error"]["code"] in (4101, 4104)
