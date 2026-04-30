"""Inbound widget.* events update the per-session WidgetRegistry."""

import threading
import types

from tui_gateway import server, widget_runtime


def _seed_session(sid="sess-a", key="key-a"):
    state = server._state()
    state.sessions[sid] = {
        "session_key": key,
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "transport": types.SimpleNamespace(write=lambda *a, **k: True),
        "client_capabilities": ["widget.render"],
        "widget_registry": widget_runtime.WidgetRegistry(),
    }
    server._register_session(sid)
    return sid, state.sessions[sid]


def test_widget_mounted_marks_card_mounted():
    sid, sess = _seed_session("sess-mount", "key-mount")
    reg = sess["widget_registry"]
    cid = reg.allocate(source="x", capabilities=[], title=None, initial_size=None, trace_id=None)

    server.dispatch({
        "jsonrpc": "2.0",
        "method": "event",
        "params": {
            "type": "widget.mounted",
            "session_id": sid,
            "payload": {"card_id": cid, "compiled_size": 1024, "compile_ms": 8},
        },
    })

    status, payload = reg.wait_for_mount(cid, timeout=0.5)
    assert status == "mounted"
    assert payload == {"compiled_size": 1024, "compile_ms": 8}


def test_widget_error_resolves_with_error():
    sid, sess = _seed_session("sess-err", "key-err")
    reg = sess["widget_registry"]
    cid = reg.allocate(source="x", capabilities=[], title=None, initial_size=None, trace_id=None)

    server.dispatch({
        "jsonrpc": "2.0",
        "method": "event",
        "params": {
            "type": "widget.error",
            "session_id": sid,
            "payload": {
                "card_id": cid, "phase": "compile", "kind": "syntax_error",
                "message": "Unexpected token at line 8", "stack": "...",
            },
        },
    })

    status, payload = reg.wait_for_mount(cid, timeout=0.5)
    assert status == "error"
    assert payload["phase"] == "compile"
    assert payload["kind"] == "syntax_error"


def test_widget_disposed_clears_card():
    sid, sess = _seed_session("sess-disp", "key-disp")
    reg = sess["widget_registry"]
    cid = reg.allocate(source="x", capabilities=[], title=None, initial_size=None, trace_id=None)

    server.dispatch({
        "jsonrpc": "2.0",
        "method": "event",
        "params": {
            "type": "widget.disposed",
            "session_id": sid,
            "payload": {"card_id": cid, "reason": "user_closed"},
        },
    })

    assert reg.get(cid) is None


def test_inbound_handler_unknown_session_does_not_raise():
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "event",
        "params": {
            "type": "widget.mounted",
            "session_id": "ghost-session",
            "payload": {"card_id": "wgt_000000", "compiled_size": 0, "compile_ms": 0},
        },
    })
    # No assertion — must not raise. Logged at warning level.
