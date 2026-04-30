"""Inbound widget.api_cancel: lookup correlation, call agent.interrupt(), pop from registry."""

import threading
import types

from tui_gateway import server, widget_runtime


def _seed(sid="sess-cx", key="key-cx"):
    state = server._state()
    state.sessions[sid] = {
        "session_key": key,
        "transport": types.SimpleNamespace(write=lambda *a, **k: True),
        "client_capabilities": ["widget.render"],
        "widget_registry": widget_runtime.WidgetRegistry(),
        "api_call_registry": widget_runtime.ApiCallRegistry(),
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
    }
    server._register_session(sid)
    return sid, state.sessions[sid]


def test_api_cancel_calls_interrupt_on_agent_ref():
    sid, sess = _seed("sess-cx-int", "key-cx-int")
    interrupted = {"called": False, "msg": None}

    class _FakeAgent:
        def interrupt(self, message=None):
            interrupted["called"] = True
            interrupted["msg"] = message

    api_reg = sess["api_call_registry"]
    cid = sess["widget_registry"].allocate(
        source="x",
        capabilities=["hermes.ask"],
        title=None,
        initial_size=None,
        trace_id=None,
    )
    api_reg.register(
        correlation_id="corr_cx",
        card_id=cid,
        capability="hermes.ask",
        agent_ref=_FakeAgent(),
    )

    server.dispatch(
        {
            "jsonrpc": "2.0",
            "method": "event",
            "params": {
                "type": "widget.api_cancel",
                "session_id": sid,
                "payload": {
                    "correlation_id": "corr_cx",
                    "card_id": cid,
                    "reason": "card_disposed",
                },
            },
        }
    )

    assert interrupted["called"] is True
    # Correlation removed from active map.
    assert api_reg.get("corr_cx") is None


def test_api_cancel_for_unknown_correlation_is_silent():
    sid, sess = _seed("sess-cx-unk", "key-cx-unk")
    server.dispatch(
        {
            "jsonrpc": "2.0",
            "method": "event",
            "params": {
                "type": "widget.api_cancel",
                "session_id": sid,
                "payload": {
                    "correlation_id": "corr_ghost",
                    "card_id": "wgt_x",
                    "reason": "user_cancelled",
                },
            },
        }
    )
    # No assertion — just must not raise.


def test_api_cancel_records_reason_for_observability():
    sid, sess = _seed("sess-cx-reas", "key-cx-reas")
    api_reg = sess["api_call_registry"]
    cid = sess["widget_registry"].allocate(
        source="x",
        capabilities=["hermes.ask"],
        title=None,
        initial_size=None,
        trace_id=None,
    )
    entry = api_reg.register(
        correlation_id="corr_obs",
        card_id=cid,
        capability="hermes.ask",
        agent_ref=None,
    )

    server.dispatch(
        {
            "jsonrpc": "2.0",
            "method": "event",
            "params": {
                "type": "widget.api_cancel",
                "session_id": sid,
                "payload": {
                    "correlation_id": "corr_obs",
                    "card_id": cid,
                    "reason": "card_updated",
                },
            },
        }
    )

    # Entry was popped — but reason was recorded on the snapshot.
    assert entry.cancel_reason == "card_updated"
    assert entry.cancelled_at is not None
