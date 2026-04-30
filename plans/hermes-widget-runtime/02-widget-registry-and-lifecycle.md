# Plan 02 — WidgetRegistry, Render/Update/Message/Dispose Lifecycle, Inbound Event Dispatch

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The agent can render and dispose a widget end-to-end against the Tauri client. Card-id allocation, source emission, mount/error resolution, source updates, JSON-message delivery, and idempotent disposal all work; client-emitted events flow back through a typed-event dispatch path.

**Architecture:** Add a per-session `WidgetRegistry` to `tui_gateway/widget_runtime.py` keyed by `card_id` (`wgt_<6 hex>`). The registry owns each card's mount-resolution future so `render_widget` can block on `widget.mounted` (success) or `widget.error` (failure). Add a typed-event dispatch path in `tui_gateway/server.py:dispatch()` so inbound `event`-shape messages from clients (`widget.mounted`, `widget.error`, `widget.disposed`) reach handler functions. Handlers mutate the per-session registry. Replace the four lifecycle stubs in `tools/widget_tools.py` with real implementations that look up the session via `session_id` kwarg.

**Tech Stack:** Python 3.11, `secrets.token_hex`, `threading.Event`, `concurrent.futures.Future` for blocking on mount, existing `_emit` / `_state` / `current_transport` machinery, pytest via `scripts/run_tests.sh`.

---

## File structure

**Create:**
- `tests/tui_gateway/test_widget_registry.py` — registry contract tests.
- `tests/tui_gateway/test_inbound_event_dispatch.py` — `event`-method routing in `dispatch()`.
- `tests/tools/test_widget_tools_lifecycle.py` — `render_widget`/`widget_update`/`widget_message`/`widget_dispose` integration with the registry and `_emit`.

**Modify:**
- `tui_gateway/widget_runtime.py` — add `CardEntry` dataclass, `WidgetRegistry` class, helper `_resolve_session_by_key`.
- `tui_gateway/server.py` — extend `dispatch()` to route `method == "event"` to a `_event_handlers` map; add `event_handler` decorator; register handlers for `widget.mounted` / `widget.error` / `widget.disposed`; ensure `_init_session` allocates an empty `WidgetRegistry` on the session dict; on `_unregister_session` (or session.close), tear down the registry.
- `tools/widget_tools.py` — replace four stubs with real implementations.

---

## Task 1: `WidgetRegistry` data model

**Files:**
- Modify: `tui_gateway/widget_runtime.py`
- Test: `tests/tui_gateway/test_widget_registry.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/tui_gateway/test_widget_registry.py
"""WidgetRegistry: card-id allocation, mount resolution, idempotent disposal."""

import re
import threading
import time

import pytest

from tui_gateway.widget_runtime import WidgetRegistry, CardEntry


CARD_ID_RE = re.compile(r"^wgt_[0-9a-f]{6}$")


def test_allocate_returns_well_formed_card_id():
    reg = WidgetRegistry()
    card_id = reg.allocate(source="x", capabilities=[], title=None, initial_size=None, trace_id=None)
    assert CARD_ID_RE.match(card_id)


def test_allocate_returns_unique_ids():
    reg = WidgetRegistry()
    seen = {reg.allocate(source="x", capabilities=[], title=None, initial_size=None, trace_id=None) for _ in range(50)}
    assert len(seen) == 50


def test_get_returns_entry_for_live_card():
    reg = WidgetRegistry()
    cid = reg.allocate(source="src", capabilities=["hermes.ask"], title="t", initial_size={"w": 400, "h": 300}, trace_id="tc_1")
    entry = reg.get(cid)
    assert isinstance(entry, CardEntry)
    assert entry.card_id == cid
    assert entry.capabilities == ["hermes.ask"]
    assert entry.title == "t"
    assert entry.initial_size == {"w": 400, "h": 300}
    assert entry.trace_id == "tc_1"


def test_get_returns_none_for_unknown_card():
    reg = WidgetRegistry()
    assert reg.get("wgt_000000") is None


def test_wait_for_mount_resolves_when_marked_mounted():
    reg = WidgetRegistry()
    cid = reg.allocate(source="x", capabilities=[], title=None, initial_size=None, trace_id=None)

    def mount_later():
        time.sleep(0.05)
        reg.mark_mounted(cid, compiled_size=4823, compile_ms=12)

    threading.Thread(target=mount_later, daemon=True).start()
    result = reg.wait_for_mount(cid, timeout=2.0)
    assert result == ("mounted", {"compiled_size": 4823, "compile_ms": 12})


def test_wait_for_mount_resolves_with_error():
    reg = WidgetRegistry()
    cid = reg.allocate(source="x", capabilities=[], title=None, initial_size=None, trace_id=None)

    def err_later():
        time.sleep(0.05)
        reg.mark_error(cid, phase="compile", kind="syntax_error", message="oops", stack="trace")

    threading.Thread(target=err_later, daemon=True).start()
    status, payload = reg.wait_for_mount(cid, timeout=2.0)
    assert status == "error"
    assert payload["phase"] == "compile"
    assert payload["kind"] == "syntax_error"
    assert payload["message"] == "oops"


def test_wait_for_mount_times_out():
    reg = WidgetRegistry()
    cid = reg.allocate(source="x", capabilities=[], title=None, initial_size=None, trace_id=None)
    result = reg.wait_for_mount(cid, timeout=0.05)
    assert result == ("timeout", None)


def test_dispose_returns_true_for_live_card():
    reg = WidgetRegistry()
    cid = reg.allocate(source="x", capabilities=[], title=None, initial_size=None, trace_id=None)
    disposed, already = reg.dispose(cid, reason="task_complete")
    assert disposed is True
    assert already is False
    assert reg.get(cid) is None


def test_dispose_is_idempotent_on_unknown_card():
    reg = WidgetRegistry()
    disposed, already = reg.dispose("wgt_000000", reason="task_complete")
    assert disposed is False
    assert already is True


def test_dispose_is_idempotent_on_already_disposed_card():
    reg = WidgetRegistry()
    cid = reg.allocate(source="x", capabilities=[], title=None, initial_size=None, trace_id=None)
    reg.dispose(cid, reason="task_complete")
    disposed, already = reg.dispose(cid, reason="task_complete")
    assert disposed is False
    assert already is True


def test_update_source_on_live_card():
    reg = WidgetRegistry()
    cid = reg.allocate(source="old", capabilities=["hermes.ask"], title="t", initial_size=None, trace_id=None)
    updated, gone = reg.update_source(cid, source="new", capabilities=["notes.save"])
    assert updated is True
    assert gone is False
    entry = reg.get(cid)
    assert entry.source == "new"
    assert entry.capabilities == ["notes.save"]


def test_update_source_preserves_capabilities_when_omitted():
    reg = WidgetRegistry()
    cid = reg.allocate(source="old", capabilities=["hermes.ask"], title="t", initial_size=None, trace_id=None)
    reg.update_source(cid, source="new", capabilities=None)
    assert reg.get(cid).capabilities == ["hermes.ask"]


def test_update_source_on_disposed_card_signals_gone():
    reg = WidgetRegistry()
    updated, gone = reg.update_source("wgt_000000", source="x", capabilities=None)
    assert updated is False
    assert gone is True
```

- [ ] **Step 2: Run tests to verify they fail**

```
scripts/run_tests.sh tests/tui_gateway/test_widget_registry.py -v
```

Expected: FAIL — `WidgetRegistry` and `CardEntry` not defined.

- [ ] **Step 3: Implement the registry**

Append to `tui_gateway/widget_runtime.py`:

```python
import secrets
import threading
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CardEntry:
    card_id: str
    source: str
    capabilities: list
    title: Optional[str]
    initial_size: Optional[dict]
    trace_id: Optional[str]
    # Internal: signaled when widget.mounted or widget.error arrives.
    _resolved: threading.Event = field(default_factory=threading.Event)
    # Set when resolution arrives. ("mounted", payload) or ("error", payload).
    _resolution: Optional[tuple] = None


class WidgetRegistry:
    """Per-session registry of live widget cards.

    Owns:
      - card_id allocation (wgt_<6 hex>)
      - source/capability metadata for validation of incoming widget.api_call
      - mount-resolution Event so render_widget can block on widget.mounted
        / widget.error from the client
      - idempotent disposal
    """

    def __init__(self) -> None:
        self._cards: dict[str, CardEntry] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _new_card_id() -> str:
        return f"wgt_{secrets.token_hex(3)}"

    def allocate(
        self,
        source: str,
        capabilities: list,
        title: Optional[str],
        initial_size: Optional[dict],
        trace_id: Optional[str],
    ) -> str:
        with self._lock:
            while True:
                cid = self._new_card_id()
                if cid not in self._cards:
                    break
            self._cards[cid] = CardEntry(
                card_id=cid,
                source=source,
                capabilities=list(capabilities or []),
                title=title,
                initial_size=initial_size,
                trace_id=trace_id,
            )
            return cid

    def get(self, card_id: str) -> Optional[CardEntry]:
        with self._lock:
            return self._cards.get(card_id)

    def mark_mounted(self, card_id: str, compiled_size: int, compile_ms: int) -> None:
        with self._lock:
            entry = self._cards.get(card_id)
            if entry is None:
                return
            entry._resolution = (
                "mounted",
                {"compiled_size": int(compiled_size), "compile_ms": int(compile_ms)},
            )
            entry._resolved.set()

    def mark_error(
        self, card_id: str, phase: str, kind: str, message: str, stack: str = ""
    ) -> None:
        with self._lock:
            entry = self._cards.get(card_id)
            if entry is None:
                return
            entry._resolution = (
                "error",
                {"phase": phase, "kind": kind, "message": message, "stack": stack},
            )
            entry._resolved.set()

    def wait_for_mount(self, card_id: str, timeout: float):
        with self._lock:
            entry = self._cards.get(card_id)
        if entry is None:
            return ("timeout", None)
        ok = entry._resolved.wait(timeout=timeout)
        if not ok:
            return ("timeout", None)
        return entry._resolution or ("timeout", None)

    def update_source(
        self,
        card_id: str,
        source: str,
        capabilities: Optional[list],
    ) -> tuple[bool, bool]:
        """Return (updated, card_gone)."""
        with self._lock:
            entry = self._cards.get(card_id)
            if entry is None:
                return (False, True)
            entry.source = source
            if capabilities is not None:
                entry.capabilities = list(capabilities)
            return (True, False)

    def dispose(self, card_id: str, reason: str) -> tuple[bool, bool]:
        """Return (disposed, already_disposed)."""
        with self._lock:
            entry = self._cards.pop(card_id, None)
            if entry is None:
                return (False, True)
            if not entry._resolved.is_set():
                # Unblock any pending wait_for_mount with a synthetic disposal signal.
                entry._resolution = ("error", {"phase": "dispose", "kind": "disposed_before_mount", "message": "card disposed before mount resolved", "stack": ""})
                entry._resolved.set()
            return (True, False)
```

- [ ] **Step 4: Run tests to verify they pass**

```
scripts/run_tests.sh tests/tui_gateway/test_widget_registry.py -v
```

Expected: 14 passed.

- [ ] **Step 5: Commit**

```
git add tui_gateway/widget_runtime.py tests/tui_gateway/test_widget_registry.py
git commit -m "feat(tui_gateway): add WidgetRegistry with mount-future and idempotent disposal"
```

---

## Task 2: Inbound event dispatch path in `tui_gateway/server.py`

**Files:**
- Modify: `tui_gateway/server.py:480-520` (around `handle_request` and `dispatch`).
- Test: `tests/tui_gateway/test_inbound_event_dispatch.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/tui_gateway/test_inbound_event_dispatch.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

```
scripts/run_tests.sh tests/tui_gateway/test_inbound_event_dispatch.py -v
```

Expected: FAIL — `_event_handlers` and `event_handler` decorator do not exist; the event-shape message currently hits `handle_request` and produces a `-32601 unknown method: event` response.

- [ ] **Step 3: Add the event-dispatch path**

In `tui_gateway/server.py`, alongside the existing `_methods` dict and `method` decorator (around line 472-484), add:

```python
_event_handlers: dict[str, callable] = {}


def event_handler(name: str):
    """Register a handler for inbound client → server event-shape messages.

    Handler signature: ``handler(params: dict) -> None``. ``params`` is
    the full ``params`` field of the event message — typically containing
    ``type``, ``session_id``, and ``payload``.
    """
    def dec(fn):
        _event_handlers[name] = fn
        return fn
    return dec
```

Modify `dispatch()` so it short-circuits `method == "event"` BEFORE the long-handler check:

```python
def dispatch(req: dict, transport: Optional[Transport] = None) -> dict | None:
    t = transport or _stdio_transport
    token = bind_transport(t)
    try:
        # Inbound client → server events have no id and need no response.
        # Route them to typed-event handlers; drop unknown event types
        # silently rather than producing a -32601.
        if req.get("method") == "event":
            params = req.get("params") or {}
            handler = _event_handlers.get(params.get("type", ""))
            if handler is not None:
                try:
                    handler(params)
                except Exception as exc:
                    logger = logging.getLogger(__name__)
                    logger.warning("event handler %s raised: %s", params.get("type"), exc)
            return None

        if req.get("method") not in _LONG_HANDLERS:
            return handle_request(req)

        # ... existing pool-dispatch logic unchanged
```

(`logging` is already imported at the top of `server.py`; if not, add it.)

- [ ] **Step 4: Run tests to verify they pass**

```
scripts/run_tests.sh tests/tui_gateway/test_inbound_event_dispatch.py tests/test_tui_gateway_server.py -v
```

Expected: new tests pass; existing dispatcher tests still pass.

- [ ] **Step 5: Commit**

```
git add tui_gateway/server.py tests/tui_gateway/test_inbound_event_dispatch.py
git commit -m "feat(tui_gateway): typed-event dispatch path for inbound client events"
```

---

## Task 3: Inbound `widget.mounted` / `widget.error` / `widget.disposed` handlers

**Files:**
- Modify: `tui_gateway/widget_runtime.py` — register the three event handlers at module import.
- Modify: `tui_gateway/server.py:_init_session` — initialize an empty `WidgetRegistry` on the session dict.
- Test: extend `tests/tui_gateway/test_inbound_event_dispatch.py` (or add a new file) for end-to-end registry mutation.

- [ ] **Step 1: Write the failing test**

```python
# tests/tui_gateway/test_widget_inbound_handlers.py
"""Inbound widget.* events update the per-session WidgetRegistry."""

import threading
import types

from tui_gateway import server, widget_runtime


def _seed_session(sid="sess-a", key="key-a"):
    state = server._state()
    state.sessions[sid] = {
        "session_key": key,
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "transport": types.SimpleNamespace(write=lambda *a, **k: True),
        "client_capabilities": ["widget.render"],
        "widget_registry": widget_runtime.WidgetRegistry(),
    }
    server._register_session(sid)
    return sid, state.sessions[sid]


def test_widget_mounted_marks_card_mounted():
    sid, sess = _seed_session("sess-mount", "key-mount")
    reg = sess["widget_registry"]
    cid = reg.allocate(source="x", capabilities=[], title=None, initial_size=None, trace_id=None)

    server.dispatch({
        "jsonrpc": "2.0",
        "method": "event",
        "params": {
            "type": "widget.mounted",
            "session_id": sid,
            "payload": {"card_id": cid, "compiled_size": 1024, "compile_ms": 8},
        },
    })

    status, payload = reg.wait_for_mount(cid, timeout=0.5)
    assert status == "mounted"
    assert payload == {"compiled_size": 1024, "compile_ms": 8}


def test_widget_error_resolves_with_error():
    sid, sess = _seed_session("sess-err", "key-err")
    reg = sess["widget_registry"]
    cid = reg.allocate(source="x", capabilities=[], title=None, initial_size=None, trace_id=None)

    server.dispatch({
        "jsonrpc": "2.0",
        "method": "event",
        "params": {
            "type": "widget.error",
            "session_id": sid,
            "payload": {
                "card_id": cid, "phase": "compile", "kind": "syntax_error",
                "message": "Unexpected token at line 8", "stack": "...",
            },
        },
    })

    status, payload = reg.wait_for_mount(cid, timeout=0.5)
    assert status == "error"
    assert payload["phase"] == "compile"
    assert payload["kind"] == "syntax_error"


def test_widget_disposed_clears_card():
    sid, sess = _seed_session("sess-disp", "key-disp")
    reg = sess["widget_registry"]
    cid = reg.allocate(source="x", capabilities=[], title=None, initial_size=None, trace_id=None)

    server.dispatch({
        "jsonrpc": "2.0",
        "method": "event",
        "params": {
            "type": "widget.disposed",
            "session_id": sid,
            "payload": {"card_id": cid, "reason": "user_closed"},
        },
    })

    assert reg.get(cid) is None


def test_inbound_handler_unknown_session_does_not_raise():
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "event",
        "params": {
            "type": "widget.mounted",
            "session_id": "ghost-session",
            "payload": {"card_id": "wgt_000000", "compiled_size": 0, "compile_ms": 0},
        },
    })
    # No assertion — must not raise. Logged at warning level.
```

- [ ] **Step 2: Run tests to verify they fail**

```
scripts/run_tests.sh tests/tui_gateway/test_widget_inbound_handlers.py -v
```

Expected: FAIL — handlers not registered; events drop silently.

- [ ] **Step 3: Register the inbound handlers**

Append to `tui_gateway/widget_runtime.py`:

```python
def _registry_for(session_id: str) -> Optional["WidgetRegistry"]:
    """Look up the per-session WidgetRegistry by sid. Returns None if no session."""
    from tui_gateway.server import _state_for_session

    state = _state_for_session(session_id)
    sess = state.sessions.get(session_id) or {}
    return sess.get("widget_registry")


def _register_inbound_event_handlers() -> None:
    """Wire the three inbound widget.* events into tui_gateway.server.

    Called from server module init so the handlers exist before any
    client connects.
    """
    from tui_gateway.server import event_handler

    @event_handler("widget.mounted")
    def _on_mounted(params: dict) -> None:
        sid = params.get("session_id", "")
        payload = params.get("payload") or {}
        reg = _registry_for(sid)
        if reg is None:
            return
        reg.mark_mounted(
            payload.get("card_id", ""),
            compiled_size=int(payload.get("compiled_size", 0) or 0),
            compile_ms=int(payload.get("compile_ms", 0) or 0),
        )

    @event_handler("widget.error")
    def _on_error(params: dict) -> None:
        sid = params.get("session_id", "")
        payload = params.get("payload") or {}
        reg = _registry_for(sid)
        if reg is None:
            return
        reg.mark_error(
            payload.get("card_id", ""),
            phase=str(payload.get("phase", "unknown")),
            kind=str(payload.get("kind", "unknown")),
            message=str(payload.get("message", "")),
            stack=str(payload.get("stack", "")),
        )

    @event_handler("widget.disposed")
    def _on_disposed(params: dict) -> None:
        sid = params.get("session_id", "")
        payload = params.get("payload") or {}
        reg = _registry_for(sid)
        if reg is None:
            return
        reg.dispose(payload.get("card_id", ""), reason=str(payload.get("reason", "user_closed")))
```

In `tui_gateway/server.py`, add a call to `_register_inbound_event_handlers()` near the bottom of the module (after `_event_handlers` is defined):

```python
# At the bottom of server.py, after all method handlers are registered:
from tui_gateway.widget_runtime import _register_inbound_event_handlers as _reg_widget_inbound
_reg_widget_inbound()
```

- [ ] **Step 4: Allocate a `WidgetRegistry` on every session at init**

In `tui_gateway/server.py:_init_session` (around line 1419), add to the session dict initializer:

```python
state.sessions[sid] = {
    ...,
    "widget_registry": __import__("tui_gateway.widget_runtime", fromlist=["WidgetRegistry"]).WidgetRegistry(),
}
```

Or import at the top of the module: `from tui_gateway.widget_runtime import WidgetRegistry as _WidgetRegistry`, then use `_WidgetRegistry()` in the dict literal.

Do the same in `session.create` (line 1566) and `session.resume`/`session.branch` session-dict constructors so every session has a registry from the start.

- [ ] **Step 5: Run tests to verify they pass**

```
scripts/run_tests.sh tests/tui_gateway/test_widget_inbound_handlers.py tests/tui_gateway/test_inbound_event_dispatch.py -v
```

Expected: 4 + 4 = 8 passed.

- [ ] **Step 6: Commit**

```
git add tui_gateway/widget_runtime.py tui_gateway/server.py tests/tui_gateway/test_widget_inbound_handlers.py
git commit -m "feat(tui_gateway): wire widget.mounted/error/disposed inbound handlers"
```

---

## Task 4: `render_widget` real implementation

**Files:**
- Modify: `tools/widget_tools.py`
- Test: `tests/tools/test_widget_tools_lifecycle.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/tools/test_widget_tools_lifecycle.py
"""render_widget allocates a card_id, emits widget.render, blocks on mount."""

import json
import re
import threading
import types

import pytest

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
```

- [ ] **Step 2: Run tests to verify they fail**

```
scripts/run_tests.sh tests/tools/test_widget_tools_lifecycle.py -v
```

Expected: FAIL — `render_widget` is still the stub.

- [ ] **Step 3: Implement `render_widget`**

Update `tools/widget_tools.py`:

```python
import json
from typing import Any, Optional

from tools.registry import registry
from tui_gateway.widget_runtime import (
    is_widget_render_available,
    WidgetRegistry,
)


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

SOURCE_BYTE_CAP = 256 * 1024
MESSAGE_BYTE_CAP = 256 * 1024
RENDER_TIMEOUT_S = 10.0

ALLOWED_CAPABILITIES = {
    "hermes.ask",
    "notes.save",
    "storage.get",
    "storage.set",
    "storage.keys",
    "card.resize",
    "card.set_title",
    "card.close",
    "os.notify",
    "os.copy_clipboard",
}


# --------------------------------------------------------------------------
# Session lookup
# --------------------------------------------------------------------------


def _resolve_session(session_key: str) -> Optional[tuple[str, dict]]:
    """Map session_key (HERMES_SESSION_KEY) → (sid, session_dict).

    Iterates _state().sessions. Process-wide session count is small; the
    O(n) scan is fine in the hot path.
    """
    from tui_gateway.server import _state

    state = _state()
    for sid, sess in state.sessions.items():
        if sess.get("session_key") == session_key:
            return sid, sess
    return None


def _err(code: int, message: str, **extra) -> str:
    payload = {"error": {"code": code, "message": message, **extra}}
    return json.dumps(payload, ensure_ascii=False)


def _emit_widget_event(event_type: str, sid: str, payload: dict) -> None:
    from tui_gateway.server import _emit

    _emit(event_type, sid, payload)


# --------------------------------------------------------------------------
# render_widget
# --------------------------------------------------------------------------


def _render_widget(args: dict, **kwargs: Any) -> str:
    session_key = kwargs.get("session_id", "") or ""
    resolved = _resolve_session(session_key)
    if resolved is None:
        return _err(4001, "session not found")
    sid, sess = resolved

    source = args.get("source") or ""
    capabilities = args.get("capabilities") or []
    title = args.get("title")
    initial_size = args.get("initial_size")

    if len(source.encode("utf-8")) > SOURCE_BYTE_CAP:
        return _err(4102, f"source exceeds {SOURCE_BYTE_CAP} bytes")
    unknown = [c for c in capabilities if c not in ALLOWED_CAPABILITIES]
    if unknown:
        return _err(4101, f"unknown capabilities: {unknown}")

    reg: WidgetRegistry = sess["widget_registry"]
    card_id = reg.allocate(
        source=source,
        capabilities=capabilities,
        title=title,
        initial_size=initial_size,
        trace_id=kwargs.get("tool_call_id"),
    )

    payload = {
        "card_id": card_id,
        "source": source,
        "capabilities": list(capabilities),
    }
    if title is not None:
        payload["title"] = title
    if initial_size is not None:
        payload["initial_size"] = initial_size
    if kwargs.get("tool_call_id"):
        payload["trace_id"] = kwargs["tool_call_id"]

    _emit_widget_event("widget.render", sid, payload)

    status, info = reg.wait_for_mount(card_id, timeout=RENDER_TIMEOUT_S)
    if status == "mounted":
        return json.dumps({"card_id": card_id, "compiled_size": info.get("compiled_size", 0), "compile_ms": info.get("compile_ms", 0)}, ensure_ascii=False)
    if status == "error":
        return _err(
            5101,
            info.get("message", "client refused to mount"),
            phase=info.get("phase", "compile"),
            kind=info.get("kind", "unknown"),
            card_id=card_id,
        )
    # timeout
    return _err(5102, f"render_widget timed out after {RENDER_TIMEOUT_S}s waiting for widget.mounted", card_id=card_id)
```

Replace the `render_widget` registration in the `_REGISTRATIONS` loop so its handler is `_render_widget`:

```python
def _handler_for(name: str):
    return {
        "render_widget": _render_widget,
        # widget_update, widget_message, widget_dispose: filled in Tasks 5-7
    }.get(name) or (lambda args, _tname=name, **kw: _stub(_tname))


for _name, _schema in _REGISTRATIONS:
    registry.register(
        name=_name,
        toolset="widget",
        schema=_schema,
        handler=_handler_for(_name),
        check_fn=_check,
        emoji="🪟",
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```
scripts/run_tests.sh tests/tools/test_widget_tools_lifecycle.py::test_render_widget_returns_card_id_after_mount tests/tools/test_widget_tools_lifecycle.py::test_render_widget_returns_error_on_mount_error tests/tools/test_widget_tools_lifecycle.py::test_render_widget_rejects_oversized_source tests/tools/test_widget_tools_lifecycle.py::test_render_widget_rejects_unknown_capability tests/tools/test_widget_tools_lifecycle.py::test_render_widget_times_out -v
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```
git add tools/widget_tools.py tests/tools/test_widget_tools_lifecycle.py
git commit -m "feat(tools): real render_widget — emit widget.render and block on mount"
```

---

## Task 5: `widget_update` real implementation

**Files:**
- Modify: `tools/widget_tools.py`
- Test: extend `tests/tools/test_widget_tools_lifecycle.py`

- [ ] **Step 1: Add the failing tests**

```python
# Append to tests/tools/test_widget_tools_lifecycle.py

def test_widget_update_emits_widget_update_and_returns_updated(monkeypatch, session):
    sid, key, sess = session
    emits = []
    monkeypatch.setattr(server, "_emit", lambda *a: emits.append(a))
    reg = sess["widget_registry"]
    cid = reg.allocate(source="old", capabilities=["hermes.ask"], title=None, initial_size=None, trace_id=None)

    result = _call(
        "widget_update",
        {"card_id": cid, "source": "new"},
        session_id=key,
    )
    assert result == {"updated": True, "card_gone": False}
    assert reg.get(cid).source == "new"
    assert any(e[0] == "widget.update" and e[1] == sid and e[2]["card_id"] == cid for e in emits)


def test_widget_update_signals_card_gone_for_unknown(monkeypatch, session):
    sid, key, _ = session
    monkeypatch.setattr(server, "_emit", lambda *a: None)
    result = _call(
        "widget_update",
        {"card_id": "wgt_deadbe", "source": "x"},
        session_id=key,
    )
    assert result == {"updated": False, "card_gone": True}


def test_widget_update_propagates_capabilities_when_provided(monkeypatch, session):
    sid, key, sess = session
    monkeypatch.setattr(server, "_emit", lambda *a: None)
    reg = sess["widget_registry"]
    cid = reg.allocate(source="old", capabilities=["hermes.ask"], title=None, initial_size=None, trace_id=None)
    _call(
        "widget_update",
        {"card_id": cid, "source": "new", "capabilities": ["notes.save"]},
        session_id=key,
    )
    assert reg.get(cid).capabilities == ["notes.save"]
```

- [ ] **Step 2: Run tests to verify they fail**

```
scripts/run_tests.sh tests/tools/test_widget_tools_lifecycle.py -k widget_update -v
```

Expected: FAIL.

- [ ] **Step 3: Implement `widget_update`**

Add to `tools/widget_tools.py`:

```python
def _widget_update(args: dict, **kwargs: Any) -> str:
    session_key = kwargs.get("session_id", "") or ""
    resolved = _resolve_session(session_key)
    if resolved is None:
        return _err(4001, "session not found")
    sid, sess = resolved

    card_id = args.get("card_id") or ""
    source = args.get("source") or ""
    capabilities = args.get("capabilities")

    if len(source.encode("utf-8")) > SOURCE_BYTE_CAP:
        return _err(4102, f"source exceeds {SOURCE_BYTE_CAP} bytes")
    if capabilities is not None:
        unknown = [c for c in capabilities if c not in ALLOWED_CAPABILITIES]
        if unknown:
            return _err(4101, f"unknown capabilities: {unknown}")

    reg: WidgetRegistry = sess["widget_registry"]
    updated, gone = reg.update_source(card_id, source=source, capabilities=capabilities)
    if updated:
        payload = {"card_id": card_id, "source": source}
        if capabilities is not None:
            payload["capabilities"] = list(capabilities)
        _emit_widget_event("widget.update", sid, payload)
    return json.dumps({"updated": updated, "card_gone": gone}, ensure_ascii=False)
```

Add it to `_handler_for`:

```python
def _handler_for(name: str):
    return {
        "render_widget": _render_widget,
        "widget_update": _widget_update,
        # widget_message, widget_dispose: Tasks 6-7
    }.get(name) or (lambda args, _tname=name, **kw: _stub(_tname))
```

- [ ] **Step 4: Run tests to verify they pass**

```
scripts/run_tests.sh tests/tools/test_widget_tools_lifecycle.py -k widget_update -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```
git add tools/widget_tools.py tests/tools/test_widget_tools_lifecycle.py
git commit -m "feat(tools): real widget_update — replace source and capability allowlist"
```

---

## Task 6: `widget_message` real implementation

**Files:**
- Modify: `tools/widget_tools.py`
- Test: extend `tests/tools/test_widget_tools_lifecycle.py`

- [ ] **Step 1: Add failing tests**

```python
# Append to tests/tools/test_widget_tools_lifecycle.py

def test_widget_message_emits_and_returns_delivered(monkeypatch, session):
    sid, key, sess = session
    emits = []
    monkeypatch.setattr(server, "_emit", lambda *a: emits.append(a))
    reg = sess["widget_registry"]
    cid = reg.allocate(source="x", capabilities=[], title=None, initial_size=None, trace_id=None)

    result = _call(
        "widget_message",
        {"card_id": cid, "payload": {"kind": "data.refresh", "rows": [1, 2, 3]}},
        session_id=key,
    )
    assert result == {"delivered": True, "card_gone": False}
    msg_emit = next(e for e in emits if e[0] == "widget.message")
    assert msg_emit[1] == sid
    assert msg_emit[2]["card_id"] == cid
    assert msg_emit[2]["message"]["kind"] == "data.refresh"


def test_widget_message_signals_card_gone(monkeypatch, session):
    sid, key, _ = session
    monkeypatch.setattr(server, "_emit", lambda *a: None)
    result = _call(
        "widget_message",
        {"card_id": "wgt_deadbe", "payload": {"x": 1}},
        session_id=key,
    )
    assert result == {"delivered": False, "card_gone": True}


def test_widget_message_rejects_oversized_payload(monkeypatch, session):
    sid, key, sess = session
    monkeypatch.setattr(server, "_emit", lambda *a: None)
    reg = sess["widget_registry"]
    cid = reg.allocate(source="x", capabilities=[], title=None, initial_size=None, trace_id=None)
    huge = {"data": "x" * (260 * 1024)}
    result = _call(
        "widget_message",
        {"card_id": cid, "payload": huge},
        session_id=key,
    )
    assert result["error"]["code"] == 4107
```

- [ ] **Step 2: Run tests to verify they fail**

```
scripts/run_tests.sh tests/tools/test_widget_tools_lifecycle.py -k widget_message -v
```

Expected: FAIL.

- [ ] **Step 3: Implement `widget_message`**

```python
def _widget_message(args: dict, **kwargs: Any) -> str:
    session_key = kwargs.get("session_id", "") or ""
    resolved = _resolve_session(session_key)
    if resolved is None:
        return _err(4001, "session not found")
    sid, sess = resolved

    card_id = args.get("card_id") or ""
    payload = args.get("payload")
    if payload is None:
        return _err(4012, "payload required")
    serialized = json.dumps(payload, ensure_ascii=False)
    if len(serialized.encode("utf-8")) > MESSAGE_BYTE_CAP:
        return _err(4107, f"widget.message payload exceeds {MESSAGE_BYTE_CAP} bytes")

    reg: WidgetRegistry = sess["widget_registry"]
    entry = reg.get(card_id)
    if entry is None:
        return json.dumps({"delivered": False, "card_gone": True}, ensure_ascii=False)

    _emit_widget_event(
        "widget.message",
        sid,
        {"card_id": card_id, "message": payload},
    )
    return json.dumps({"delivered": True, "card_gone": False}, ensure_ascii=False)
```

Add to `_handler_for`. Update the dispatcher dict.

- [ ] **Step 4: Run tests to verify they pass**

```
scripts/run_tests.sh tests/tools/test_widget_tools_lifecycle.py -k widget_message -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```
git add tools/widget_tools.py tests/tools/test_widget_tools_lifecycle.py
git commit -m "feat(tools): real widget_message — push JSON payload into live card"
```

---

## Task 7: `widget_dispose` real implementation (idempotent both sides)

**Files:**
- Modify: `tools/widget_tools.py`
- Test: extend `tests/tools/test_widget_tools_lifecycle.py`

- [ ] **Step 1: Add failing tests**

```python
# Append to tests/tools/test_widget_tools_lifecycle.py

def test_widget_dispose_emits_dispose_and_returns_disposed(monkeypatch, session):
    sid, key, sess = session
    emits = []
    monkeypatch.setattr(server, "_emit", lambda *a: emits.append(a))
    reg = sess["widget_registry"]
    cid = reg.allocate(source="x", capabilities=[], title=None, initial_size=None, trace_id=None)

    result = _call(
        "widget_dispose",
        {"card_id": cid, "reason": "task_complete"},
        session_id=key,
    )
    assert result == {"disposed": True, "already_disposed": False}
    assert reg.get(cid) is None
    dispose_emit = next(e for e in emits if e[0] == "widget.dispose")
    assert dispose_emit[1] == sid
    assert dispose_emit[2]["card_id"] == cid
    assert dispose_emit[2]["reason"] == "task_complete"


def test_widget_dispose_idempotent_for_already_disposed(monkeypatch, session):
    sid, key, sess = session
    emits = []
    monkeypatch.setattr(server, "_emit", lambda *a: emits.append(a))
    reg = sess["widget_registry"]
    cid = reg.allocate(source="x", capabilities=[], title=None, initial_size=None, trace_id=None)
    reg.dispose(cid, reason="user_closed")  # client got there first

    result = _call(
        "widget_dispose",
        {"card_id": cid},
        session_id=key,
    )
    assert result == {"disposed": False, "already_disposed": True}
    # No widget.dispose emit for an already-disposed card.
    assert not any(e[0] == "widget.dispose" for e in emits)


def test_widget_dispose_idempotent_for_unknown_card(monkeypatch, session):
    sid, key, _ = session
    monkeypatch.setattr(server, "_emit", lambda *a: None)
    result = _call(
        "widget_dispose",
        {"card_id": "wgt_neverexisted"},
        session_id=key,
    )
    assert result == {"disposed": False, "already_disposed": True}
```

- [ ] **Step 2: Run tests to verify they fail**

```
scripts/run_tests.sh tests/tools/test_widget_tools_lifecycle.py -k widget_dispose -v
```

- [ ] **Step 3: Implement `widget_dispose`**

```python
def _widget_dispose(args: dict, **kwargs: Any) -> str:
    session_key = kwargs.get("session_id", "") or ""
    resolved = _resolve_session(session_key)
    if resolved is None:
        return _err(4001, "session not found")
    sid, sess = resolved

    card_id = args.get("card_id") or ""
    reason = str(args.get("reason") or "task_complete")

    reg: WidgetRegistry = sess["widget_registry"]
    disposed, already = reg.dispose(card_id, reason=reason)
    if disposed:
        _emit_widget_event(
            "widget.dispose",
            sid,
            {"card_id": card_id, "reason": reason},
        )
    return json.dumps({"disposed": disposed, "already_disposed": already}, ensure_ascii=False)
```

Add to `_handler_for`. Update the dispatcher dict.

- [ ] **Step 4: Run tests to verify they pass**

```
scripts/run_tests.sh tests/tools/test_widget_tools_lifecycle.py -v
```

Expected: all lifecycle tests pass.

- [ ] **Step 5: Commit**

```
git add tools/widget_tools.py tests/tools/test_widget_tools_lifecycle.py
git commit -m "feat(tools): real widget_dispose — idempotent close with both-sides race handling"
```

---

## Task 8: Cross-machine alignment — card-id format guarantee

**Files:**
- Test: `tests/tui_gateway/test_card_id_format_alignment.py`

- [ ] **Step 1: Add the test**

```python
# tests/tui_gateway/test_card_id_format_alignment.py
"""Card IDs from the server allocator match the validator the Tauri side uses.

Cross-machine alignment: the format /^wgt_[0-9a-f]{6}$/ is shared
verbatim. Tauri validates incoming widget.render events against this
exact regex; producing a non-matching id would crash mount.
"""

import re

from tui_gateway.widget_runtime import WidgetRegistry

CANONICAL_RE = re.compile(r"^wgt_[0-9a-f]{6}$")


def test_allocator_produces_canonical_format():
    reg = WidgetRegistry()
    for _ in range(200):
        cid = reg.allocate(source="x", capabilities=[], title=None, initial_size=None, trace_id=None)
        assert CANONICAL_RE.match(cid), f"non-canonical card_id: {cid!r}"


def test_allocator_avoids_collisions_in_one_session():
    reg = WidgetRegistry()
    seen = {reg.allocate(source="x", capabilities=[], title=None, initial_size=None, trace_id=None) for _ in range(500)}
    assert len(seen) == 500
```

- [ ] **Step 2: Run the test**

```
scripts/run_tests.sh tests/tui_gateway/test_card_id_format_alignment.py -v
```

Expected: PASS (allocator was implemented in Task 1).

- [ ] **Step 3: Commit**

```
git add tests/tui_gateway/test_card_id_format_alignment.py
git commit -m "test(tui_gateway): card_id format matches Tauri-side validator"
```

---

## Task 9: Cross-machine alignment — gateway accepts client event envelopes

**Files:**
- Test: `tests/tui_gateway/test_event_envelope_alignment.py`

- [ ] **Step 1: Add the test**

```python
# tests/tui_gateway/test_event_envelope_alignment.py
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
```

- [ ] **Step 2: Run the test**

```
scripts/run_tests.sh tests/tui_gateway/test_event_envelope_alignment.py -v
```

Expected: 2 passed.

- [ ] **Step 3: Commit**

```
git add tests/tui_gateway/test_event_envelope_alignment.py
git commit -m "test(tui_gateway): inbound widget event envelope alignment"
```

---

## Task 10: Session teardown clears the registry

**Files:**
- Modify: `tui_gateway/server.py:_unregister_session` or wherever session.close lives.
- Test: extend `tests/tui_gateway/test_widget_inbound_handlers.py`.

- [ ] **Step 1: Add failing test**

```python
# Append to tests/tui_gateway/test_widget_inbound_handlers.py

def test_session_close_clears_registry(monkeypatch):
    sid, sess = _seed_session("sess-tear", "key-tear")
    reg = sess["widget_registry"]
    cid = reg.allocate(source="x", capabilities=[], title=None, initial_size=None, trace_id=None)

    # Find session.close handler; signature varies — adapt to the actual.
    handler = server._methods.get("session.close")
    assert handler is not None, "session.close must exist"
    handler(1, {"session_id": sid})

    # State.sessions[sid] should be gone, AND any subsequent widget.disposed
    # event for that sid is a no-op (no crash, no re-add).
    assert sid not in server._state().sessions
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "event",
        "params": {
            "type": "widget.disposed",
            "session_id": sid,
            "payload": {"card_id": cid, "reason": "session_ended"},
        },
    })
```

- [ ] **Step 2: Run test to verify it fails or passes**

```
scripts/run_tests.sh tests/tui_gateway/test_widget_inbound_handlers.py::test_session_close_clears_registry -v
```

If it passes (because `state.sessions.pop(sid)` already drops the registry alongside the dict), commit and move on. If it fails, locate `session.close` (search `@method("session.close")` in `tui_gateway/server.py`) and ensure the registry is explicitly cleared by `pop`-ing the session dict — which already removes the registry. The teardown does NOT need to call `dispose` on each card individually (the iframe pool tears down when the WS disconnects on the Tauri side).

- [ ] **Step 3: Commit**

```
git add tests/tui_gateway/test_widget_inbound_handlers.py tui_gateway/server.py
git commit -m "test(tui_gateway): session close drops widget registry cleanly"
```

---

## Acceptance for Plan 02

- `WidgetRegistry` is per-session, owns card-id allocation, mount-future, and idempotent disposal.
- Inbound `widget.mounted`, `widget.error`, `widget.disposed` events route through the new `_event_handlers` map and mutate the per-session registry.
- `render_widget`: validates source size and capability whitelist, allocates card_id, emits `widget.render`, blocks on mount-future, returns `card_id` (success) or structured error (5101 mount-error / 5102 timeout / 4101 unknown cap / 4102 oversized source).
- `widget_update`: same validations; emits `widget.update` if the card is live; returns `{updated, card_gone}`.
- `widget_message`: enforces 256 KiB payload cap (4107); emits `widget.message`; returns `{delivered, card_gone}`.
- `widget_dispose`: idempotent; emits `widget.dispose` only when this call wins the race; returns `{disposed, already_disposed}`.
- Cross-machine alignment tests for card-id format and event-envelope shape pass.
- Session teardown drops the registry cleanly; late-arriving events for a closed session are no-ops.

**Demoable end-state.** With Plan 01 + Plan 02 in place, an agent connected via the Tauri client can call `render_widget`, see the card mount, push messages into it, update its source, and dispose it — all over the wire, with the WS staying responsive throughout. Plan 03 then unlocks `canvasAPI.hermes.ask` round-trips.
