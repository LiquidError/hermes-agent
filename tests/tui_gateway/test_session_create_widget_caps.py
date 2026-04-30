import types

from tui_gateway import server


def _fake_transport(caps=None):
    t = types.SimpleNamespace()
    t.write = lambda *a, **k: True
    t.client_capabilities = list(caps or [])
    return t


def test_session_create_with_widget_cap_sets_context_during_make_agent(monkeypatch):
    transport = _fake_transport(["widget.render"])
    seen = {"available": None}

    def fake_make_agent(sid, key, session_id=None):
        from tui_gateway import widget_runtime

        seen["available"] = widget_runtime.is_widget_render_available()
        return types.SimpleNamespace(model="x", get_total_tokens=lambda: 0)

    monkeypatch.setattr(server, "_make_agent", fake_make_agent)
    monkeypatch.setattr(server, "current_transport", lambda: transport)
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)

    handler = server._methods["session.create"]
    resp = handler(1, {})
    sid = resp["result"]["session_id"]
    state = server._state()
    state.sessions[sid]["agent_ready"].wait(timeout=5.0)

    assert seen["available"] is True
    assert state.sessions[sid].get("client_capabilities") == ["widget.render"]


def test_session_create_without_widget_cap_keeps_context_false(monkeypatch):
    transport = _fake_transport([])
    seen = {"available": None}

    def fake_make_agent(sid, key, session_id=None):
        from tui_gateway import widget_runtime

        seen["available"] = widget_runtime.is_widget_render_available()
        return types.SimpleNamespace(model="x", get_total_tokens=lambda: 0)

    monkeypatch.setattr(server, "_make_agent", fake_make_agent)
    monkeypatch.setattr(server, "current_transport", lambda: transport)
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)

    handler = server._methods["session.create"]
    resp = handler(1, {})
    sid = resp["result"]["session_id"]
    state = server._state()
    state.sessions[sid]["agent_ready"].wait(timeout=5.0)

    assert seen["available"] is False
    assert state.sessions[sid].get("client_capabilities") == []
