"""Cross-machine: gateway accepts the exact event envelope the Tauri side emits.

The Tauri client emits widget.* events as:
  {"jsonrpc": "2.0", "method": "event", "params": {"type": ..., "session_id": ..., "payload": {...}}}
with NO id field. The gateway must route them and emit no response.
"""

import threading
import types

from tui_gateway import server, widget_runtime


def test_no_id_field_does_not_produce_response():
    sid = "sess-env"
    state = server._state()
    state.sessions[sid] = {
        "session_key": "key-env",
        "transport": types.SimpleNamespace(write=lambda *a, **k: True),
        "client_capabilities": ["widget.render"],
        "widget_registry": widget_runtime.WidgetRegistry(),
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
    }
    server._register_session(sid)
    cid = state.sessions[sid]["widget_registry"].allocate(source="x", capabilities=[], title=None, initial_size=None, trace_id=None)

    resp = server.dispatch({
        "jsonrpc": "2.0",
        "method": "event",
        "params": {
            "type": "widget.disposed",
            "session_id": sid,
            "payload": {"card_id": cid, "reason": "user_closed"},
        },
    })
    assert resp is None, "events must not produce a response"


def test_all_four_widget_inbound_events_route():
    """widget.mounted, widget.error, widget.disposed (this plan) plus
    widget.api_cancel (Plan 04) must all have registered handlers.
    Plan 02 ships three of four; this test guards the trio.
    """
    expected = {"widget.mounted", "widget.error", "widget.disposed"}
    assert expected.issubset(set(server._event_handlers))
