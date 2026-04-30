"""dispatch() routes method=='event' messages to a typed-event handler map."""

from __future__ import annotations

from tui_gateway import server


def test_event_message_with_known_type_invokes_handler(monkeypatch):
    seen = []

    @server.event_handler("widget.mounted")
    def _handler(params):
        seen.append(params)

    try:
        resp = server.dispatch({
            "jsonrpc": "2.0",
            "method": "event",
            "params": {
                "type": "widget.mounted",
                "session_id": "ab12cd34",
                "payload": {"card_id": "wgt_8a3f9c", "compiled_size": 4823, "compile_ms": 12},
            },
        })
    finally:
        server._event_handlers.pop("widget.mounted", None)

    # Events have no id — dispatch returns None and emits no response.
    assert resp is None
    assert len(seen) == 1
    assert seen[0]["type"] == "widget.mounted"
    assert seen[0]["payload"]["card_id"] == "wgt_8a3f9c"


def test_event_message_with_unknown_type_is_silently_dropped():
    # Unknown event types must NOT produce a -32601 method-not-found
    # response — that's the bug we're fixing. Drop them silently.
    resp = server.dispatch({
        "jsonrpc": "2.0",
        "method": "event",
        "params": {"type": "totally.unknown.event", "session_id": "x"},
    })
    assert resp is None


def test_event_handler_decorator_registers_into_module_map():
    @server.event_handler("plan02.test.event")
    def _h(params):
        pass

    try:
        assert server._event_handlers["plan02.test.event"] is _h
    finally:
        server._event_handlers.pop("plan02.test.event", None)


def test_request_method_still_routes_normally():
    # Regression guard — adding the event path must not break method dispatch.
    resp = server.dispatch({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "totally.unknown.method",
        "params": {},
    })
    assert resp is not None
    assert resp["error"]["code"] == -32601
