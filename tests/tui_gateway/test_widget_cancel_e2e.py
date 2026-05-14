"""End-to-end: render, kick off hermes.ask, dispose mid-flight, no widget.api_response."""

import json
import threading
import time
import types

import tools.widget_tools  # noqa: F401  — registers widget tools
from tools.registry import registry
from tui_gateway import server, widget_runtime


def test_render_then_dispose_mid_ask(monkeypatch):
    sid, key = "sess-e2e", "key-e2e"
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

    emits = []
    monkeypatch.setattr(server, "_emit", lambda *a: emits.append(a))

    # Render: mock the mount.
    reg = state.sessions[sid]["widget_registry"]

    def fake_mount():
        for _ in range(100):
            with reg._lock:
                cards = list(reg._cards.keys())
            if cards:
                reg.mark_mounted(cards[0], compiled_size=1024, compile_ms=8)
                return
            threading.Event().wait(0.01)

    threading.Thread(target=fake_mount, daemon=True).start()
    render_handler = registry.get_entry("render_widget").handler
    render_result = json.loads(
        render_handler(
            {
                "source": "export default function C(){return null}",
                "capabilities": ["hermes.ask"],
            },
            session_id=key,
        )
    )
    cid = render_result["card_id"]

    # Kick off a slow widget.api_call.
    proceed = threading.Event()

    class _SlowAgent:
        def __init__(self, *a, **kw):
            pass

        def interrupt(self, message=None):
            pass

        def run_conversation(self, text, conversation_history=None):
            proceed.wait(timeout=5.0)
            return {"final_response": "answer that arrives too late"}

    monkeypatch.setattr("run_agent.AIAgent", _SlowAgent)

    api_call_handler = server._methods["widget.api_call"]
    ack = api_call_handler(
        99,
        {
            "session_id": sid,
            "card_id": cid,
            "correlation_id": "corr_e2e",
            "capability": "hermes.ask",
            "args": {"prompt": "x"},
        },
    )
    assert ack["result"]["accepted"] is True
    time.sleep(0.05)  # let worker start

    # Dispose the card mid-flight.
    dispose_handler = registry.get_entry("widget_dispose").handler
    json.loads(
        dispose_handler({"card_id": cid, "reason": "task_complete"}, session_id=key)
    )

    # widget.api_cancel emitted for the correlation.
    assert any(
        e[0] == "widget.api_cancel" and e[2]["correlation_id"] == "corr_e2e"
        for e in emits
    )

    # Now release the slow agent — its response must be dropped.
    proceed.set()
    time.sleep(0.2)
    assert not any(
        e[0] == "widget.api_response" for e in emits
    ), "cancelled correlation must not emit a widget.api_response"
