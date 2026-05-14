"""Cross-machine: widget.api_cancel envelope shape (client → server and server → client).

Spec §3.5.4 — payload carries exactly {correlation_id, card_id, reason}.
The Tauri side sends + receives this exact shape; deviating breaks the wire.
"""

import json
import threading
import types

import tools.widget_tools  # noqa: F401  — registers widget tools
from tools.registry import registry
from tui_gateway import server, widget_runtime


def _setup(sid="sess-env-cx", key="key-env-cx"):
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
    return sid, key, state.sessions[sid]


def test_outbound_widget_api_cancel_payload_shape(monkeypatch):
    """Outbound emit on card disposal carries exactly the three fields."""
    sid, key, sess = _setup("sess-env-out", "key-env-out")
    cid = sess["widget_registry"].allocate(
        source="x",
        capabilities=["hermes.ask"],
        title=None,
        initial_size=None,
        trace_id=None,
    )
    sess["api_call_registry"].register(
        correlation_id="corr_e",
        card_id=cid,
        capability="hermes.ask",
        agent_ref=None,
    )

    emits = []
    monkeypatch.setattr(server, "_emit", lambda *a: emits.append(a))

    handler = registry.get_entry("widget_dispose").handler
    json.loads(handler({"card_id": cid, "reason": "task_complete"}, session_id=key))

    api_cancels = [e for e in emits if e[0] == "widget.api_cancel"]
    assert len(api_cancels) == 1
    payload = api_cancels[0][2]
    assert set(payload.keys()) == {"correlation_id", "card_id", "reason"}
    assert payload["correlation_id"] == "corr_e"
    assert payload["card_id"] == cid
    assert payload["reason"] == "card_disposed"


def test_inbound_widget_api_cancel_envelope_routes_correctly():
    """Inbound: jsonrpc=2.0, method=event, no id, params.type=widget.api_cancel."""
    sid, key, sess = _setup("sess-env-in", "key-env-in")
    cid = sess["widget_registry"].allocate(
        source="x",
        capabilities=["hermes.ask"],
        title=None,
        initial_size=None,
        trace_id=None,
    )
    sess["api_call_registry"].register(
        correlation_id="corr_in",
        card_id=cid,
        capability="hermes.ask",
        agent_ref=None,
    )

    resp = server.dispatch(
        {
            "jsonrpc": "2.0",
            "method": "event",
            # NO id field — matches Tauri-side outbound envelope.
            "params": {
                "type": "widget.api_cancel",
                "session_id": sid,
                "payload": {
                    "correlation_id": "corr_in",
                    "card_id": cid,
                    "reason": "user_cancelled",
                },
            },
        }
    )
    assert resp is None  # events have no response
    # Correlation is gone from the registry.
    assert sess["api_call_registry"].get("corr_in") is None


def test_all_four_widget_inbound_events_route():
    """All four widget.* event types must be registered: mounted, error, disposed, api_cancel."""
    expected = {"widget.mounted", "widget.error", "widget.disposed", "widget.api_cancel"}
    assert expected.issubset(set(server._event_handlers))
