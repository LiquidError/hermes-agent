"""End-to-end: client.hello → session.create → AIAgent sees widget tools iff cap advertised.

Cross-machine alignment: the six widget tools must register together as a
bundle conditional on widget.render. Half-and-half is a wire break.
"""

import types

from tui_gateway import server


WIDGET_TOOLS = {
    "render_widget", "widget_update", "widget_message", "widget_dispose",
    "list_widget_examples", "read_widget_example",
}


def _run_session_create(monkeypatch, capabilities):
    transport = types.SimpleNamespace(write=lambda *a, **k: True)
    transport.client_capabilities = list(capabilities)
    captured = {}

    def fake_make_agent(sid, key, session_id=None):
        from run_agent import AIAgent
        from model_tools import get_tool_definitions

        agent = AIAgent.__new__(AIAgent)
        tools = get_tool_definitions(["widget"], quiet_mode=True)
        agent.valid_tool_names = {t["function"]["name"] for t in tools}
        captured["tools"] = agent.valid_tool_names
        agent.model = "x"
        agent.get_total_tokens = lambda: 0
        return agent

    monkeypatch.setattr(server, "_make_agent", fake_make_agent)
    monkeypatch.setattr(server, "current_transport", lambda: transport)
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)

    handler = server._methods["session.create"]
    resp = handler(99, {})
    sid = resp["result"]["session_id"]
    server._state().sessions[sid]["agent_ready"].wait(timeout=5.0)
    return captured["tools"]


def test_all_six_register_when_cap_advertised(monkeypatch):
    tools = _run_session_create(monkeypatch, ["widget.render"])
    assert WIDGET_TOOLS.issubset(tools)


def test_none_register_when_cap_absent(monkeypatch):
    tools = _run_session_create(monkeypatch, [])
    assert WIDGET_TOOLS.isdisjoint(tools)


def test_bundle_is_atomic_with_cap(monkeypatch):
    tools = _run_session_create(monkeypatch, ["widget.render"])
    visible = WIDGET_TOOLS & tools
    assert visible == WIDGET_TOOLS or visible == set(), (
        f"widget tools must register as a bundle, got partial: {visible}"
    )
