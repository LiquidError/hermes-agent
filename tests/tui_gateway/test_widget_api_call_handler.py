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


import json


def test_worker_emits_api_response_on_success(monkeypatch):
    sid, key, sess, cid = _seed_session_with_card(sid="sess-ws", key="key-ws")
    emits = []
    monkeypatch.setattr(server, "_emit", lambda *a: emits.append(a))

    # Stub AIAgent.run_conversation to return a deterministic answer.
    class _FakeAgent:
        def __init__(self, *a, **kw):
            self.session_id = kw.get("session_id", "")
        def run_conversation(self, text, conversation_history=None):
            return {"final_response": "Q3 revenue was $4.2M, up 18% YoY."}
    monkeypatch.setattr("run_agent.AIAgent", _FakeAgent)

    server._spawn_widget_api_call_worker(
        sid=sid,
        session_key=key,
        correlation_id="corr_ok",
        card_id=cid,
        capability="hermes.ask",
        call_args={"prompt": "What's Q3 revenue?"},
        history_snapshot=[],
    )

    # Worker runs sync if we don't actually thread; but Plan 03 does
    # spawn a thread. Wait briefly.
    for _ in range(100):
        if emits:
            break
        threading.Event().wait(0.02)

    assert any(e[0] == "widget.api_response" for e in emits), f"no widget.api_response in {emits!r}"
    resp = next(e for e in emits if e[0] == "widget.api_response")
    assert resp[1] == sid
    assert resp[2]["correlation_id"] == "corr_ok"
    assert resp[2]["card_id"] == cid
    assert resp[2]["result"]["answer"] == "Q3 revenue was $4.2M, up 18% YoY."
    # Correlation popped from registry.
    assert sess["api_call_registry"].get("corr_ok") is None


def test_worker_emits_error_4106_when_response_exceeds_cap(monkeypatch):
    sid, key, sess, cid = _seed_session_with_card(sid="sess-cap", key="key-cap")
    emits = []
    monkeypatch.setattr(server, "_emit", lambda *a: emits.append(a))

    huge = "x" * (40 * 1024)  # 40 KiB > 32 KiB cap
    class _FakeAgent:
        def __init__(self, *a, **kw): pass
        def run_conversation(self, text, conversation_history=None):
            return {"final_response": huge}
    monkeypatch.setattr("run_agent.AIAgent", _FakeAgent)

    server._spawn_widget_api_call_worker(
        sid=sid, session_key=key, correlation_id="corr_big",
        card_id=cid, capability="hermes.ask", call_args={"prompt": "long"},
        history_snapshot=[],
    )
    for _ in range(100):
        if emits:
            break
        threading.Event().wait(0.02)

    assert emits, "no widget.api_response emitted"
    resp = next(e for e in emits if e[0] == "widget.api_response")
    assert "error" in resp[2]
    assert resp[2]["error"]["code"] == 4106
    assert "32" in resp[2]["error"]["message"], "error should reference the 32 KiB cap"
    # Actual size mentioned for diagnostics.
    assert "40" in resp[2]["error"]["message"] or "actual" in resp[2]["error"]["message"].lower()


def test_worker_handles_agent_exception(monkeypatch):
    sid, key, sess, cid = _seed_session_with_card(sid="sess-exc", key="key-exc")
    emits = []
    monkeypatch.setattr(server, "_emit", lambda *a: emits.append(a))

    class _FakeAgent:
        def __init__(self, *a, **kw): pass
        def run_conversation(self, text, conversation_history=None):
            raise RuntimeError("agent blew up")
    monkeypatch.setattr("run_agent.AIAgent", _FakeAgent)

    server._spawn_widget_api_call_worker(
        sid=sid, session_key=key, correlation_id="corr_err",
        card_id=cid, capability="hermes.ask", call_args={"prompt": "x"},
        history_snapshot=[],
    )
    for _ in range(100):
        if emits:
            break
        threading.Event().wait(0.02)

    resp = next(e for e in emits if e[0] == "widget.api_response")
    assert "error" in resp[2]
    # Use the cross-side ERROR_API_CALL_EXPIRED or a similar 5xxx code.
    assert resp[2]["error"]["code"] >= 5000
