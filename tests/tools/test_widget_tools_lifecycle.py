"""render_widget allocates a card_id, emits widget.render, blocks on mount."""

import json
import re
import threading
import types

import pytest

from tools import widget_tools  # noqa: F401  triggers registration
from tools.registry import registry
from tui_gateway import server, widget_runtime


CARD_ID_RE = re.compile(r"^wgt_[0-9a-f]{6}$")


@pytest.fixture
def session(monkeypatch):
    sid, key = "sess-tool", "key-tool"
    transport = types.SimpleNamespace(write=lambda *a, **k: True)
    state = server._state()
    state.sessions[sid] = {
        "session_key": key,
        "transport": transport,
        "client_capabilities": ["widget.render"],
        "widget_registry": widget_runtime.WidgetRegistry(),
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
    }
    server._register_session(sid)
    yield sid, key, state.sessions[sid]
    state.sessions.pop(sid, None)
    server._unregister_session(sid)


def _call(name, args, session_id):
    entry = registry.get_entry(name)
    return json.loads(entry.handler(args, session_id=session_id))


def test_render_widget_returns_card_id_after_mount(monkeypatch, session):
    sid, key, sess = session
    emits = []
    monkeypatch.setattr(server, "_emit", lambda *a: emits.append(a))

    reg = sess["widget_registry"]

    def fake_mount():
        # Simulate Tauri replying with widget.mounted shortly after.
        # We poll the registry to find the card_id allocated by the tool.
        for _ in range(100):
            with reg._lock:
                cards = list(reg._cards.keys())
            if cards:
                reg.mark_mounted(cards[0], compiled_size=1024, compile_ms=8)
                return
            threading.Event().wait(0.01)

    threading.Thread(target=fake_mount, daemon=True).start()

    result = _call(
        "render_widget",
        {"source": "export default function C(){return null}", "capabilities": ["hermes.ask"], "title": "T"},
        session_id=key,
    )
    assert "card_id" in result
    assert CARD_ID_RE.match(result["card_id"])

    # widget.render emitted to the right session_id (sid, not key)
    assert any(e[0] == "widget.render" and e[1] == sid for e in emits)
    # Payload carries source + capabilities + the same card_id
    rendered = next(e for e in emits if e[0] == "widget.render")
    assert rendered[2]["card_id"] == result["card_id"]
    assert rendered[2]["source"].startswith("export default")
    assert rendered[2]["capabilities"] == ["hermes.ask"]
    assert rendered[2]["title"] == "T"


def test_render_widget_returns_error_on_mount_error(monkeypatch, session):
    sid, key, sess = session
    monkeypatch.setattr(server, "_emit", lambda *a: None)

    reg = sess["widget_registry"]

    def fake_error():
        for _ in range(100):
            with reg._lock:
                cards = list(reg._cards.keys())
            if cards:
                reg.mark_error(cards[0], phase="compile", kind="syntax_error", message="Unexpected token", stack="...")
                return
            threading.Event().wait(0.01)

    threading.Thread(target=fake_error, daemon=True).start()

    result = _call(
        "render_widget",
        {"source": "{{{not jsx}}}", "capabilities": []},
        session_id=key,
    )
    assert "error" in result
    assert result["error"]["code"] == 5101
    assert result["error"]["phase"] == "compile"
    assert "Unexpected token" in result["error"]["message"]


def test_render_widget_rejects_oversized_source(monkeypatch, session):
    sid, key, _ = session
    monkeypatch.setattr(server, "_emit", lambda *a: None)
    big = "x" * (256 * 1024 + 1)
    result = _call(
        "render_widget",
        {"source": big, "capabilities": []},
        session_id=key,
    )
    assert result["error"]["code"] == 4102


def test_render_widget_rejects_unknown_capability(monkeypatch, session):
    sid, key, _ = session
    monkeypatch.setattr(server, "_emit", lambda *a: None)
    result = _call(
        "render_widget",
        {"source": "x", "capabilities": ["bogus.thing"]},
        session_id=key,
    )
    assert result["error"]["code"] == 4101


def test_render_widget_times_out(monkeypatch, session):
    sid, key, _ = session
    monkeypatch.setattr(server, "_emit", lambda *a: None)
    # No fake_mount thread — wait for default 10s timeout. Override via env.
    import tools.widget_tools as wt
    monkeypatch.setattr(wt, "RENDER_TIMEOUT_S", 0.1)
    result = _call(
        "render_widget",
        {"source": "x", "capabilities": []},
        session_id=key,
    )
    assert result["error"]["code"] == 5102
