"""Card disposal cancels every correlation associated with that card.

Server-initiated dispose (widget_dispose tool): emit widget.dispose AND
emit widget.api_cancel for each in-flight correlation, AND remove from registry.

Client-initiated disposal (widget.disposed inbound event): same registry
clear, but no outbound widget.api_cancel — the client already knows.
"""

import json
import threading
import types

from tools.registry import registry
from tui_gateway import server, widget_runtime


def _setup(sid="sess-disp-cx", key="key-disp-cx"):
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


def test_widget_dispose_tool_cancels_inflight_calls(monkeypatch):
    sid, key, sess, cid = _setup("sess-tool-disp", "key-tool-disp")
    emits = []
    monkeypatch.setattr(server, "_emit", lambda *a: emits.append(a))

    interrupted = []

    class _FakeAgent:
        def __init__(self, name):
            self.name = name

        def interrupt(self, message=None):
            interrupted.append(self.name)

    api_reg = sess["api_call_registry"]
    api_reg.register(
        correlation_id="corr_a",
        card_id=cid,
        capability="hermes.ask",
        agent_ref=_FakeAgent("a"),
    )
    api_reg.register(
        correlation_id="corr_b",
        card_id=cid,
        capability="hermes.ask",
        agent_ref=_FakeAgent("b"),
    )
    other_cid = sess["widget_registry"].allocate(
        source="y",
        capabilities=["hermes.ask"],
        title=None,
        initial_size=None,
        trace_id=None,
    )
    api_reg.register(
        correlation_id="corr_c",
        card_id=other_cid,
        capability="hermes.ask",
        agent_ref=_FakeAgent("c"),
    )

    handler = registry.get_entry("widget_dispose").handler
    result = json.loads(
        handler({"card_id": cid, "reason": "task_complete"}, session_id=key)
    )
    assert result["disposed"] is True

    # widget.dispose emitted once for the card.
    assert any(e[0] == "widget.dispose" and e[2]["card_id"] == cid for e in emits)
    # widget.api_cancel emitted for each correlation tied to the disposed card.
    cancels = [e for e in emits if e[0] == "widget.api_cancel"]
    cancelled_corrs = {e[2]["correlation_id"] for e in cancels}
    assert cancelled_corrs == {"corr_a", "corr_b"}
    for c in cancels:
        assert c[2]["card_id"] == cid
        assert c[2]["reason"] == "card_disposed"
    # The other card's correlation is untouched.
    assert api_reg.get("corr_c") is not None
    # interrupts called for the right agents.
    assert sorted(interrupted) == ["a", "b"]


def test_inbound_widget_disposed_cancels_inflight_silently(monkeypatch):
    """When the user closes a card client-side, the client emits widget.disposed.
    Server-side, on receiving widget.disposed we silently cancel correlations
    (no outbound widget.api_cancel — the client already knows the card is gone)."""
    sid, key, sess, cid = _setup("sess-cli-disp", "key-cli-disp")
    emits = []
    monkeypatch.setattr(server, "_emit", lambda *a: emits.append(a))

    interrupted = []

    class _FakeAgent:
        def interrupt(self, message=None):
            interrupted.append(True)

    api_reg = sess["api_call_registry"]
    api_reg.register(
        correlation_id="corr_d",
        card_id=cid,
        capability="hermes.ask",
        agent_ref=_FakeAgent(),
    )

    server.dispatch(
        {
            "jsonrpc": "2.0",
            "method": "event",
            "params": {
                "type": "widget.disposed",
                "session_id": sid,
                "payload": {"card_id": cid, "reason": "user_closed"},
            },
        }
    )

    assert api_reg.get("corr_d") is None
    assert interrupted == [True]
    # No outbound widget.api_cancel — the user closed it.
    assert not any(e[0] == "widget.api_cancel" for e in emits)
