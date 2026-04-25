"""Per-connection dispatcher state isolation.

Each WS connection binds its own `_DispatcherState` via a contextvar
so in-flight session state (active agents, pending callbacks) is
isolated between concurrent clients. The default state preserves the
historical singleton behavior used by the stdio TUI.
"""

from __future__ import annotations


class TestDispatcherState:
    def test_fresh_state_is_empty(self):
        from tui_gateway.server import _DispatcherState

        state = _DispatcherState()
        assert state.sessions == {}
        assert state.pending == {}
        assert state.answers == {}

    def test_states_are_independent(self):
        from tui_gateway.server import _DispatcherState

        a = _DispatcherState()
        b = _DispatcherState()
        a.sessions["sid-1"] = {"marker": "a"}
        assert "sid-1" not in b.sessions

    def test_state_helper_returns_default_when_unbound(self):
        from tui_gateway.server import _default_state, _state

        assert _state() is _default_state

    def test_state_var_binding_is_observable(self):
        from tui_gateway.server import _DispatcherState, _state, _state_var

        bound = _DispatcherState()
        token = _state_var.set(bound)
        try:
            assert _state() is bound
        finally:
            _state_var.reset(token)

    def test_state_var_binding_is_per_context(self):
        # Reset after a binding restores the default — bindings don't leak.
        from tui_gateway.server import _DispatcherState, _default_state, _state, _state_var

        bound = _DispatcherState()
        token = _state_var.set(bound)
        _state_var.reset(token)
        assert _state() is _default_state
