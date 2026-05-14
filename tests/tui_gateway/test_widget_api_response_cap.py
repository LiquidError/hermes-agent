"""Cross-machine alignment: widget.api_response is hard-capped at 32 KiB.

The Tauri client assumes it never receives an oversized payload. A
regression here would crash iframe rendering or trigger client-side
DoS guards. Tested with three boundary cases.
"""

import json
import threading
import types

from tui_gateway import server, widget_runtime
from tui_gateway.widget_constants import HERMES_ASK_RESPONSE_CAP_BYTES


def _setup_session(sid="sess-cap-align", key="key-cap-align"):
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
    cid = state.sessions[sid]["widget_registry"].allocate(
        source="x", capabilities=["hermes.ask"], title=None, initial_size=None, trace_id=None,
    )
    return sid, key, state.sessions[sid], cid


def _run_worker_with_response_size(monkeypatch, size_bytes: int):
    sid, key, sess, cid = _setup_session(f"sess-{size_bytes}", f"key-{size_bytes}")
    emits = []
    monkeypatch.setattr(server, "_emit", lambda *a: emits.append(a))

    answer = "x" * size_bytes
    class _FakeAgent:
        def __init__(self, *a, **kw): pass
        def run_conversation(self, text, conversation_history=None):
            return {"final_response": answer}
    monkeypatch.setattr("run_agent.AIAgent", _FakeAgent)

    server._spawn_widget_api_call_worker(
        sid=sid, session_key=key, correlation_id=f"corr_{size_bytes}",
        card_id=cid, capability="hermes.ask", call_args={"prompt": "x"},
        history_snapshot=[],
    )
    for _ in range(150):
        if emits:
            break
        threading.Event().wait(0.02)
    return next(e for e in emits if e[0] == "widget.api_response")


def test_under_cap_emits_success(monkeypatch):
    # answer is ~30 KiB; serialized {"answer": "..."} adds ~12 bytes; total well under 32 KiB.
    resp = _run_worker_with_response_size(monkeypatch, 30 * 1024)
    assert "result" in resp[2]
    assert "error" not in resp[2]


def test_at_boundary_does_not_overshoot(monkeypatch):
    # Construct an answer such that serialized result is exactly the cap.
    overhead = len(json.dumps({"answer": ""}, ensure_ascii=False).encode("utf-8"))
    answer_size = HERMES_ASK_RESPONSE_CAP_BYTES - overhead
    resp = _run_worker_with_response_size(monkeypatch, answer_size)
    assert "result" in resp[2], "boundary case (== cap) must succeed"


def test_over_cap_emits_4106_with_actual_and_cap(monkeypatch):
    resp = _run_worker_with_response_size(monkeypatch, 50 * 1024)
    assert "error" in resp[2]
    assert resp[2]["error"]["code"] == 4106
    msg = resp[2]["error"]["message"]
    assert "32" in msg, "must mention the cap so the agent can react"
    # Actual size also present for diagnosability.
    assert any(token in msg for token in ("50", "actual", str(50 * 1024 + 12)))


def test_oversized_response_does_not_emit_success_payload(monkeypatch):
    """Most important: under no circumstances does an oversized result leak
    through. Fixing the cap test is wire-protocol-critical."""
    resp = _run_worker_with_response_size(monkeypatch, 100 * 1024)
    assert "result" not in resp[2], "oversized result must NEVER be emitted"
