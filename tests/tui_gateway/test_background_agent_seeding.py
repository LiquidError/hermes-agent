"""prompt.btw and widget.api_call hermes.ask must seed background agents
from the live session runtime, not just _resolve_model().

Without this, a background reflective call answers from a different
backend than the surrounding conversation: the operator's chosen
provider, base URL, api key, service tier, ephemeral system prompt, and
request overrides are silently dropped.
"""

from __future__ import annotations

import threading
import types

import pytest

from tui_gateway import server, widget_runtime


class _StubAgent:
    """Mimic the runtime attribute surface AIAgent exposes."""

    def __init__(self) -> None:
        self.model = "anthropic/claude-opus-4-7"
        self.base_url = "https://example.test/v1"
        self.api_key = "sk-test-runtime-key"
        self.provider = "anthropic"
        self.api_mode = "messages"
        self.acp_command = None
        self.acp_args = None
        self.enabled_toolsets = ["core"]
        self.ephemeral_system_prompt = "you are concise"
        self.providers_allowed = ["anthropic"]
        self.providers_ignored: list[str] = []
        self.providers_order: list[str] = []
        self.provider_sort = None
        self.provider_require_parameters = False
        self.provider_data_collection = None
        self.reasoning_config = {"enabled": True, "effort": "high"}
        self.service_tier = "priority"
        self.request_overrides = {"temperature": 0.2}
        self._fallback_model = "anthropic/claude-haiku-4-5-20251001"


def _seed_session(sid: str, key: str, agent) -> dict:
    state = server._state()
    transport = types.SimpleNamespace(write=lambda *a, **k: True)
    sess = {
        "agent": agent,
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
    state.sessions[sid] = sess
    server._register_session(sid)
    return sess


def test_background_agent_kwargs_pulls_runtime_from_live_agent():
    """Direct contract test on the helper: every override carried through."""
    parent = _StubAgent()
    kwargs = server._background_agent_kwargs(parent, "task_x")
    assert kwargs["base_url"] == "https://example.test/v1"
    assert kwargs["api_key"] == "sk-test-runtime-key"
    assert kwargs["provider"] == "anthropic"
    assert kwargs["api_mode"] == "messages"
    assert kwargs["service_tier"] == "priority"
    assert kwargs["ephemeral_system_prompt"] == "you are concise"
    assert kwargs["request_overrides"] == {"temperature": 0.2}
    assert kwargs["reasoning_config"] == {"enabled": True, "effort": "high"}
    assert kwargs["fallback_model"] == "anthropic/claude-haiku-4-5-20251001"


def test_widget_hermes_ask_seeds_agent_from_live_session(monkeypatch):
    """widget.api_call hermes.ask spawns an AIAgent that inherits the parent
    session's provider, base URL, api key, and service tier — not just the
    bare _resolve_model() result.
    """
    sid, key = "sess-runtime", "key-runtime"
    parent = _StubAgent()
    sess = _seed_session(sid, key, parent)
    cid = sess["widget_registry"].allocate(
        source="x", capabilities=["hermes.ask"],
        title=None, initial_size=None, trace_id=None,
    )

    captured: dict = {}

    class _CaptureAgent:
        def __init__(self, **kw):
            captured.update(kw)
            self.session_id = kw.get("session_id")

        def run_conversation(self, *_a, **_kw):
            return {"final_response": "stub answer"}

        def interrupt(self):
            pass

    monkeypatch.setattr("run_agent.AIAgent", _CaptureAgent)
    # Suppress the response emit — the test only inspects construction.
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)

    server._spawn_widget_api_call_worker(
        sid=sid,
        session_key=key,
        correlation_id="corr_runtime",
        card_id=cid,
        capability="hermes.ask",
        call_args={"prompt": "what?"},
        history_snapshot=[],
    )

    # Worker runs synchronously inside a daemon thread; let it complete.
    for _ in range(200):
        if "model" in captured:
            break
        threading.Event().wait(0.01)

    assert captured.get("base_url") == "https://example.test/v1"
    assert captured.get("api_key") == "sk-test-runtime-key"
    assert captured.get("provider") == "anthropic"
    assert captured.get("service_tier") == "priority"
    assert captured.get("ephemeral_system_prompt") == "you are concise"
    assert captured.get("request_overrides") == {"temperature": 0.2}
    # Capped at 8 iterations and toolless, per widget.api_call contract.
    assert captured.get("max_iterations") == 8
    assert captured.get("enabled_toolsets") == []

    # Cleanup.
    state = server._state()
    state.sessions.pop(sid, None)
    server._unregister_session(sid)
