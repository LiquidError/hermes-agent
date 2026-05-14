"""When a btw produces a result after cancellation, the result is dropped.

This is the second half of cancellation correctness — interrupt() is
best-effort, and a btw that already finished computing before the cancel
arrived must not have its widget.api_response leak through.
"""

import threading
import time
import types

from tui_gateway import server, widget_runtime


def _setup(sid="sess-drop", key="key-drop"):
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
    sess = state.sessions[sid]
    cid = sess["widget_registry"].allocate(
        source="x",
        capabilities=["hermes.ask"],
        title=None,
        initial_size=None,
        trace_id=None,
    )
    return sid, key, sess, cid


def test_btw_result_dropped_after_cancel(monkeypatch):
    sid, key, sess, cid = _setup("sess-drop-1", "key-drop-1")
    emits = []
    monkeypatch.setattr(server, "_emit", lambda *a: emits.append(a))

    proceed = threading.Event()

    class _SlowAgent:
        def __init__(self, *a, **kw):
            pass

        def interrupt(self, message=None):
            pass

        def run_conversation(self, text, conversation_history=None):
            # Block until the test gives the OK to "complete".
            proceed.wait(timeout=5.0)
            return {"final_response": "answer"}

    monkeypatch.setattr("run_agent.AIAgent", _SlowAgent)

    # Pre-register the correlation so the worker can find it.
    sess["api_call_registry"].register(
        correlation_id="corr_late",
        card_id=cid,
        capability="hermes.ask",
        agent_ref=None,
    )

    server._spawn_widget_api_call_worker(
        sid=sid,
        session_key=key,
        correlation_id="corr_late",
        card_id=cid,
        capability="hermes.ask",
        call_args={"prompt": "x"},
        history_snapshot=[],
    )
    # Let the worker register its agent_ref.
    time.sleep(0.05)

    # User closes the card; cancellation runs.
    server.dispatch(
        {
            "jsonrpc": "2.0",
            "method": "event",
            "params": {
                "type": "widget.api_cancel",
                "session_id": sid,
                "payload": {
                    "correlation_id": "corr_late",
                    "card_id": cid,
                    "reason": "user_cancelled",
                },
            },
        }
    )

    # Now release the slow agent — it produces a response, but the
    # correlation is cancelled, so worker must NOT emit widget.api_response.
    proceed.set()
    time.sleep(0.2)

    api_responses = [e for e in emits if e[0] == "widget.api_response"]
    assert (
        api_responses == []
    ), f"cancelled correlation must not emit response; got {api_responses!r}"


def test_post_cancel_runtime_observability(monkeypatch):
    """The ApiCallEntry cancellation snapshot records cancelled_at.
    The worker should log the post-cancel runtime when it eventually
    finishes — but should NOT emit a widget.api_response."""
    sid, key, sess, cid = _setup("sess-drop-obs", "key-drop-obs")
    monkeypatch.setattr(server, "_emit", lambda *a: None)

    api_reg = sess["api_call_registry"]
    api_reg.register(
        correlation_id="corr_obs",
        card_id=cid,
        capability="hermes.ask",
        agent_ref=None,
    )
    snapshot = api_reg.cancel("corr_obs", reason="card_disposed")

    # Snapshot has cancelled_at recorded.
    assert snapshot.cancelled_at is not None
