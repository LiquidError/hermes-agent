"""Session close emits widget.api_cancel(reason='session_ended') for each in-flight call."""

import threading
import types

from tui_gateway import server, widget_runtime


def test_session_close_emits_api_cancel_per_inflight(monkeypatch):
    sid, key = "sess-end", "key-end"
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
        "slash_worker": None,
    }
    server._register_session(sid)
    cid = state.sessions[sid]["widget_registry"].allocate(
        source="x",
        capabilities=["hermes.ask"],
        title=None,
        initial_size=None,
        trace_id=None,
    )
    api_reg = state.sessions[sid]["api_call_registry"]
    api_reg.register(
        correlation_id="corr_a",
        card_id=cid,
        capability="hermes.ask",
        agent_ref=None,
    )
    api_reg.register(
        correlation_id="corr_b",
        card_id=cid,
        capability="hermes.ask",
        agent_ref=None,
    )

    emits = []
    monkeypatch.setattr(server, "_emit", lambda *a: emits.append(a))

    handler = server._methods["session.close"]
    handler(1, {"session_id": sid})

    cancels = [e for e in emits if e[0] == "widget.api_cancel"]
    assert {e[2]["correlation_id"] for e in cancels} == {"corr_a", "corr_b"}
    for c in cancels:
        assert c[2]["reason"] == "session_ended"
        assert c[2]["card_id"] == cid
