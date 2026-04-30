# Plan 01 — Capability Negotiation, Tool Scaffolding, System-Prompt Addendum

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the six widget tools and the widget-author addendum visible to the agent only when the connected client advertised `widget.render` in `client.hello`. Tools register with no-op stubs; later plans replace them.

**Architecture:** The Tauri client advertises `widget.render` in its `client.hello` capabilities array. The desktop adapter records the client's capabilities onto the per-connection `WSTransport` object. When `session.create` runs on that connection, it captures the capabilities into the session dict and binds a `_WIDGET_RENDER_AVAILABLE` ContextVar before `_make_agent` constructs the `AIAgent`. The widget tools' `check_fn` reads that ContextVar so the registry filters them in/out per session. The system-prompt build conditions on `"render_widget" in self.valid_tool_names` (matching the existing `MEMORY_GUIDANCE` pattern) to append the lean addendum.

**Tech Stack:** Python 3.11, contextvars, existing `tools/registry.py` self-registration pattern, existing `tui_gateway/server.py` JSON-RPC dispatcher, `agent/prompt_builder.py` for prompt-fragment constants, pytest via `scripts/run_tests.sh`.

---

## File structure

**Create:**
- `tui_gateway/widget_runtime.py` — home for the `_WIDGET_RENDER_AVAILABLE` ContextVar plus helpers (`set_widget_render_available`, `is_widget_render_available`). Will grow in Plans 02–04 with the registries; this plan creates only the cap-gate primitives.
- `tools/widget_tools.py` — module that registers six tools (`render_widget`, `widget_update`, `widget_message`, `widget_dispose`, `list_widget_examples`, `read_widget_example`). Stubs return a `not_implemented` JSON error; subsequent plans replace the bodies.
- `assets/widget_prompts/addendum.md` — the lean (~30 line) widget-author guidance fragment.
- `assets/widget_prompts/examples/.gitkeep` — placeholder so the directory exists; Plan 05 fills it.
- `tests/gateway/test_desktop_app_widget_caps.py` — `widget.render` is advertised; client caps are stashed on the transport.
- `tests/tui_gateway/test_widget_runtime_capability_gate.py` — ContextVar set/read; default is `False`.
- `tests/tools/test_widget_tools_scaffold.py` — six tools register; `check_fn` returns `False` without context, `True` with; stubs return a structured `not_implemented` payload.
- `tests/agent/test_prompt_builder_widget.py` — `WIDGET_AUTHOR_GUIDANCE` is loaded from disk; appended iff `render_widget` in `valid_tool_names`.

**Modify:**
- `gateway/platforms/desktop_app.py` — `_register_client_hello`: add `widget.render` to advertised caps; record incoming `capabilities` onto `current_transport()`.
- `tui_gateway/server.py:session.create` — read transport's recorded caps, bind `_WIDGET_RENDER_AVAILABLE` before `_make_agent`. Same for `session.resume` and `session.branch`.
- `tui_gateway/server.py:_init_session` — store `client_capabilities` on the session dict for later plans (used by registry helpers in Plans 02–04).
- `toolsets.py` — add a `widget` toolset entry; include the six widget-tool names in `_HERMES_CORE_TOOLS` so the tui platform sees them (per-tool gating still happens via `check_fn`).
- `agent/prompt_builder.py` — add `WIDGET_AUTHOR_GUIDANCE` constant read from `assets/widget_prompts/addendum.md` at import time.
- `run_agent.py:_build_system_prompt` — append `WIDGET_AUTHOR_GUIDANCE` when `"render_widget" in self.valid_tool_names`.

---

## Task 1: ContextVar primitives in `tui_gateway/widget_runtime.py`

**Files:**
- Create: `tui_gateway/widget_runtime.py`
- Test: `tests/tui_gateway/test_widget_runtime_capability_gate.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/tui_gateway/test_widget_runtime_capability_gate.py
import threading

from tui_gateway import widget_runtime


def test_default_is_false():
    assert widget_runtime.is_widget_render_available() is False


def test_set_then_read_returns_true():
    token = widget_runtime.set_widget_render_available(True)
    try:
        assert widget_runtime.is_widget_render_available() is True
    finally:
        widget_runtime.reset_widget_render_available(token)
    assert widget_runtime.is_widget_render_available() is False


def test_per_thread_isolation_via_contextvar():
    seen = {}

    def worker():
        seen["thread"] = widget_runtime.is_widget_render_available()

    token = widget_runtime.set_widget_render_available(True)
    try:
        t = threading.Thread(target=worker)
        t.start()
        t.join()
    finally:
        widget_runtime.reset_widget_render_available(token)

    assert seen["thread"] is False
    # contextvars don't propagate to a bare threading.Thread; this
    # confirms the gate is per-context, which matches how check_fn is
    # called during agent construction.
```

- [ ] **Step 2: Run test to verify it fails**

```
scripts/run_tests.sh tests/tui_gateway/test_widget_runtime_capability_gate.py -v
```

Expected: FAIL — module `tui_gateway.widget_runtime` does not exist.

- [ ] **Step 3: Create the module**

```python
# tui_gateway/widget_runtime.py
"""Widget runtime — capability-gate primitives.

This module hosts the per-context flag the widget-tool ``check_fn``
hooks read at agent construction time. Plans 02–04 will extend it with
``WidgetRegistry`` and ``ApiCallRegistry``.
"""

from __future__ import annotations

from contextvars import ContextVar, Token

_WIDGET_RENDER_AVAILABLE: ContextVar[bool] = ContextVar(
    "widget_render_available", default=False
)


def set_widget_render_available(value: bool) -> Token:
    return _WIDGET_RENDER_AVAILABLE.set(bool(value))


def reset_widget_render_available(token: Token) -> None:
    _WIDGET_RENDER_AVAILABLE.reset(token)


def is_widget_render_available() -> bool:
    return _WIDGET_RENDER_AVAILABLE.get()
```

- [ ] **Step 4: Run test to verify it passes**

```
scripts/run_tests.sh tests/tui_gateway/test_widget_runtime_capability_gate.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```
git add tui_gateway/widget_runtime.py tests/tui_gateway/test_widget_runtime_capability_gate.py
git commit -m "feat(tui_gateway): add widget capability-gate ContextVar"
```

---

## Task 2: `client.hello` advertises `widget.render` and records client caps

**Files:**
- Modify: `gateway/platforms/desktop_app.py:81-122`
- Test: `tests/gateway/test_desktop_app_widget_caps.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/gateway/test_desktop_app_widget_caps.py
"""Widget-cap negotiation in client.hello."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gateway.platforms.desktop_app import _register_client_hello


@pytest.fixture(autouse=True)
def _reset_handler(monkeypatch):
    """Force a fresh registration so each test sees the latest handler body."""
    from gateway.platforms import desktop_app
    from tui_gateway import server as tg_server

    monkeypatch.setattr(desktop_app, "_HELLO_REGISTERED", False)
    tg_server._methods.pop("client.hello", None)
    yield


def test_server_advertises_widget_render():
    from tui_gateway import server as tg_server

    _register_client_hello()
    handler = tg_server._methods["client.hello"]
    resp = handler(1, {})
    assert "widget.render" in resp["result"]["capabilities"]


def test_records_client_widget_render_on_transport(monkeypatch):
    from tui_gateway import server as tg_server

    _register_client_hello()
    handler = tg_server._methods["client.hello"]
    fake_transport = MagicMock()
    monkeypatch.setattr(tg_server, "current_transport", lambda: fake_transport)

    handler(1, {"client_id": "tauri-test-client", "capabilities": ["widget.render"]})

    assert getattr(fake_transport, "client_capabilities", None) == ["widget.render"]


def test_no_transport_does_not_crash(monkeypatch):
    from tui_gateway import server as tg_server

    _register_client_hello()
    handler = tg_server._methods["client.hello"]
    monkeypatch.setattr(tg_server, "current_transport", lambda: None)

    resp = handler(1, {"capabilities": ["widget.render"]})
    assert "result" in resp
```

- [ ] **Step 2: Run tests to verify they fail**

```
scripts/run_tests.sh tests/gateway/test_desktop_app_widget_caps.py -v
```

Expected: FAIL — `widget.render` not in advertised caps; client caps not recorded.

- [ ] **Step 3: Update `_register_client_hello`**

In `gateway/platforms/desktop_app.py`, inside `_client_hello`, add `"widget.render"` to the `capabilities` array of `result`. After computing `client_caps`, attach it to the transport so later session-create handlers can read it:

```python
# inside _client_hello, after computing client_caps
try:
    transport = _tg_server.current_transport()
except Exception:
    transport = None
if transport is not None:
    try:
        setattr(transport, "client_capabilities", list(client_caps))
    except Exception:
        pass

result = {
    "server_version": server_version,
    "protocol_version": PROTOCOL_VERSION,
    "capabilities": [
        "voice",
        "tts",
        "approval",
        "skills",
        "insights",
        "session.list",
        "session.resume",
        "slash.exec",
        "complete.slash",
        "model.options",
        "image.attach",
        "attachment.upload",
        "config.reveal_secret",
        "message.complete",
        "tool.complete",
        "widget.render",
    ],
    ...
}
```

- [ ] **Step 4: Run tests to verify they pass**

```
scripts/run_tests.sh tests/gateway/test_desktop_app_widget_caps.py tests/gateway/test_desktop_app.py -v
```

Expected: new tests pass; existing `test_advertises_attachment_redaction_and_event_methods` still passes (this test asserts presence, not absence — adding a cap is safe).

- [ ] **Step 5: Commit**

```
git add gateway/platforms/desktop_app.py tests/gateway/test_desktop_app_widget_caps.py
git commit -m "feat(desktop_app): advertise widget.render and stash client caps on transport"
```

---

## Task 3: `session.create` binds the cap-gate before agent construction

**Files:**
- Modify: `tui_gateway/server.py:1556-1612` (around `session.create` and the `_build` thread that calls `_make_agent`).
- Modify: `tui_gateway/server.py:1419-1456` (`_init_session` — stash `client_capabilities` on the session dict).
- Test: extend `tests/test_tui_gateway_server.py` with a session.create test.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_tui_gateway_server.py` (or a new file `tests/tui_gateway/test_session_create_widget_caps.py`):

```python
# tests/tui_gateway/test_session_create_widget_caps.py
import types
from unittest.mock import patch

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
```

- [ ] **Step 2: Run tests to verify they fail**

```
scripts/run_tests.sh tests/tui_gateway/test_session_create_widget_caps.py -v
```

Expected: FAIL — context is not set; `client_capabilities` key missing on session dict.

- [ ] **Step 3: Wire the gate in `session.create`**

In `tui_gateway/server.py`, in the `session.create` handler, right after the `state.sessions[sid] = {...}` block: capture caps from the transport and stash them on the session dict. In the `_build` worker, before calling `_make_agent`, bind the widget contextvar:

```python
# tui_gateway/server.py — inside session.create

caps = list(getattr(current_transport(), "client_capabilities", []) or [])
state.sessions[sid] = {
    ...,                                  # existing fields unchanged
    "transport": current_transport() or _stdio_transport,
    "client_capabilities": caps,
}
_register_session(sid)

def _build() -> None:
    session = state.sessions.get(sid)
    if session is None:
        ready.set()
        return
    ...
    try:
        from tui_gateway.widget_runtime import set_widget_render_available, reset_widget_render_available

        widget_token = set_widget_render_available("widget.render" in caps)
        try:
            tokens = _set_session_context(key)
            try:
                agent = _make_agent(sid, key)
            finally:
                _clear_session_context(tokens)
        finally:
            reset_widget_render_available(widget_token)
    ...
```

Mirror the same pattern at the other two agent-construction sites: `session.resume` (~line 1765) and `session.branch` (~line 1967). Both also call `_make_agent`; both need the capability bound around that call. Use the same caps list captured at session-state init.

Update `_init_session` (line 1419) to also accept and store `client_capabilities` (default `[]`) — or read it from the existing `state.sessions[sid]` dict if it's already populated by the calling handler.

- [ ] **Step 4: Run tests to verify they pass**

```
scripts/run_tests.sh tests/tui_gateway/test_session_create_widget_caps.py tests/test_tui_gateway_server.py -v
```

Expected: new tests pass; existing tui_gateway tests still pass.

- [ ] **Step 5: Commit**

```
git add tui_gateway/server.py tests/tui_gateway/test_session_create_widget_caps.py
git commit -m "feat(tui_gateway): bind widget capability gate around agent construction"
```

---

## Task 4: Six widget-tool stubs in `tools/widget_tools.py`

**Files:**
- Create: `tools/widget_tools.py`
- Modify: `toolsets.py` (add tool names to `_HERMES_CORE_TOOLS` and a new `widget` toolset entry).
- Test: `tests/tools/test_widget_tools_scaffold.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/tools/test_widget_tools_scaffold.py
"""Six widget tools register, gated by capability ContextVar; stubs return not_implemented."""

import json

import pytest

from tools import widget_tools  # noqa: F401  triggers registration
from tools.registry import registry
from tui_gateway import widget_runtime


WIDGET_TOOLS = [
    "render_widget",
    "widget_update",
    "widget_message",
    "widget_dispose",
    "list_widget_examples",
    "read_widget_example",
]


def test_all_six_register_under_widget_toolset():
    for name in WIDGET_TOOLS:
        entry = registry.get_entry(name)
        assert entry is not None, f"{name} not registered"
        assert entry.toolset == "widget"


def test_check_fn_returns_false_without_context():
    for name in WIDGET_TOOLS:
        entry = registry.get_entry(name)
        assert entry.check_fn() is False, f"{name} visible without cap"


def test_check_fn_returns_true_with_context():
    token = widget_runtime.set_widget_render_available(True)
    try:
        for name in WIDGET_TOOLS:
            entry = registry.get_entry(name)
            assert entry.check_fn() is True, f"{name} hidden with cap"
    finally:
        widget_runtime.reset_widget_render_available(token)


def test_stubs_return_not_implemented():
    for name in WIDGET_TOOLS:
        entry = registry.get_entry(name)
        result = entry.handler({}, callback=None)
        payload = json.loads(result)
        assert payload.get("error") == "not_implemented"
        assert payload.get("tool") == name


def test_get_definitions_excludes_widget_tools_without_cap():
    defs = registry.get_definitions(set(WIDGET_TOOLS), quiet=True)
    assert defs == []


def test_get_definitions_includes_widget_tools_with_cap():
    token = widget_runtime.set_widget_render_available(True)
    try:
        defs = registry.get_definitions(set(WIDGET_TOOLS), quiet=True)
        assert {d["function"]["name"] for d in defs} == set(WIDGET_TOOLS)
    finally:
        widget_runtime.reset_widget_render_available(token)
```

- [ ] **Step 2: Run tests to verify they fail**

```
scripts/run_tests.sh tests/tools/test_widget_tools_scaffold.py -v
```

Expected: FAIL — `tools.widget_tools` does not exist.

- [ ] **Step 3: Create `tools/widget_tools.py`**

```python
# tools/widget_tools.py
"""Widget tools — agent-facing surface for the canvas widget runtime.

Six tools:
  render_widget          - create a card on the canvas
  widget_update          - replace the source of a live card
  widget_message         - push structured data into a live card
  widget_dispose         - close a live card
  list_widget_examples   - discover example .tsx files
  read_widget_example    - read a specific example file

This plan ships stubs returning ``{"error": "not_implemented"}``; later
plans replace each body. The capability gate (``check_fn``) and OpenAI
schemas land here so the agent sees the tools (when capable) from the
moment Plan 01 ships.
"""

from __future__ import annotations

import json
from typing import Any

from tools.registry import registry
from tui_gateway.widget_runtime import is_widget_render_available


def _stub(tool_name: str) -> str:
    return json.dumps(
        {"error": "not_implemented", "tool": tool_name},
        ensure_ascii=False,
    )


def _check() -> bool:
    return is_widget_render_available()


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------

RENDER_WIDGET_SCHEMA = {
    "name": "render_widget",
    "description": (
        "Render a custom React/JSX card on the user's canvas. The agent "
        "writes the source; the Tauri host compiles and mounts it inside "
        "a sandboxed iframe. Declares the canvasAPI capabilities the card "
        "may use. Returns the card_id string. Use list_widget_examples / "
        "read_widget_example first to learn the primitives surface."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": "JSX source. Must export a default React component. Max 256 KiB.",
            },
            "capabilities": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Names of canvasAPI methods the card may call. Subset of: "
                    "hermes.ask, notes.save, storage.get, storage.set, storage.keys, "
                    "card.resize, card.set_title, card.close, os.notify, os.copy_clipboard."
                ),
            },
            "title": {"type": "string", "description": "Human-readable card title."},
            "initial_size": {
                "type": "object",
                "properties": {
                    "w": {"type": "integer"},
                    "h": {"type": "integer"},
                },
            },
        },
        "required": ["source", "capabilities"],
    },
}

WIDGET_UPDATE_SCHEMA = {
    "name": "widget_update",
    "description": (
        "Replace the source of a live card. Card position is preserved; "
        "React state resets. Pending hermes.ask calls from the old version "
        "are cancelled. Returns {updated: bool, card_gone: bool}."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "card_id": {"type": "string"},
            "source": {"type": "string"},
            "capabilities": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["card_id", "source"],
    },
}

WIDGET_MESSAGE_SCHEMA = {
    "name": "widget_message",
    "description": (
        "Push a structured JSON message into a live card without remount. "
        "The card receives it via canvasAPI.onMessage(handler). Max 256 KiB. "
        "Returns {delivered: bool, card_gone: bool}."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "card_id": {"type": "string"},
            "payload": {"type": "object"},
        },
        "required": ["card_id", "payload"],
    },
}

WIDGET_DISPOSE_SCHEMA = {
    "name": "widget_dispose",
    "description": (
        "Close a live card. Idempotent — calling on an already-disposed "
        "card returns {disposed: false, already_disposed: true}. Use this "
        "to clean up cards whose task has completed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "card_id": {"type": "string"},
            "reason": {
                "type": "string",
                "description": "Short observability string. Suggested: task_complete, superseded, error.",
            },
        },
        "required": ["card_id"],
    },
}

LIST_WIDGET_EXAMPLES_SCHEMA = {
    "name": "list_widget_examples",
    "description": (
        "List available widget example .tsx files. Returns "
        "[{name, summary}, ...]. Call this first to pick a relevant pattern, "
        "then call read_widget_example(name) for the one(s) you want."
    ),
    "parameters": {"type": "object", "properties": {}},
}

READ_WIDGET_EXAMPLE_SCHEMA = {
    "name": "read_widget_example",
    "description": (
        "Read a specific widget example .tsx file with its inline JSDoc. "
        "Use this when about to render and need a reference for the "
        "canvasAPI / canvas-primitives surface."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Example name without .tsx extension.",
            },
        },
        "required": ["name"],
    },
}


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------

_REGISTRATIONS = (
    ("render_widget", RENDER_WIDGET_SCHEMA),
    ("widget_update", WIDGET_UPDATE_SCHEMA),
    ("widget_message", WIDGET_MESSAGE_SCHEMA),
    ("widget_dispose", WIDGET_DISPOSE_SCHEMA),
    ("list_widget_examples", LIST_WIDGET_EXAMPLES_SCHEMA),
    ("read_widget_example", READ_WIDGET_EXAMPLE_SCHEMA),
)

for _name, _schema in _REGISTRATIONS:
    registry.register(
        name=_name,
        toolset="widget",
        schema=_schema,
        handler=lambda args, _tname=_name, **kw: _stub(_tname),
        check_fn=_check,
        emoji="🪟",
    )
```

- [ ] **Step 4: Add the tools and toolset to `toolsets.py`**

In `toolsets.py`, add the six widget-tool names to `_HERMES_CORE_TOOLS` (so the tui platform sees them in the candidate set; per-tool gating still happens via `check_fn`):

```python
# at the end of _HERMES_CORE_TOOLS, before the closing `]`:
    # Canvas widget runtime (gated per-session on widget.render capability)
    "render_widget", "widget_update", "widget_message", "widget_dispose",
    "list_widget_examples", "read_widget_example",
```

Add a `widget` toolset entry to `TOOLSETS`:

```python
"widget": {
    "description": "Canvas widget runtime — render React/JSX cards on the Tauri client (gated on widget.render capability)",
    "tools": [
        "render_widget", "widget_update", "widget_message", "widget_dispose",
        "list_widget_examples", "read_widget_example",
    ],
    "includes": [],
},
```

- [ ] **Step 5: Run tests to verify they pass**

```
scripts/run_tests.sh tests/tools/test_widget_tools_scaffold.py -v
```

Expected: 6 passed.

- [ ] **Step 6: Run the full toolset-discovery sanity tests**

```
scripts/run_tests.sh tests/run_agent/ tests/tools/ -v
```

Expected: no regressions. The new tool registrations should not collide with existing tools (none share these names).

- [ ] **Step 7: Commit**

```
git add tools/widget_tools.py toolsets.py tests/tools/test_widget_tools_scaffold.py
git commit -m "feat(tools): add widget tool stubs gated by widget.render capability"
```

---

## Task 5: Lean addendum file

**Files:**
- Create: `assets/widget_prompts/addendum.md`
- Create: `assets/widget_prompts/examples/.gitkeep` (empty file so the dir exists; Plan 05 fills it)

- [ ] **Step 1: Write the addendum**

Create `assets/widget_prompts/addendum.md` with the following content. Aim for ~30 lines; lead with the heuristics, follow with import surface, end with lifecycle do's and don'ts. Do NOT inline `canvasAPI` or `canvas-primitives` types — those are read on demand via `read_widget_example`.

```markdown
# Widget rendering

You can render custom React/JSX cards onto the user's canvas via `render_widget`. Six tools are available: `render_widget`, `widget_update`, `widget_message`, `widget_dispose`, `list_widget_examples`, `read_widget_example`.

## When to render a widget

Reach for `render_widget` when:

- The task produces a bounded artifact with state — a draft, a form, a tracker, a comparison, a chart — and the user benefits from interacting with it rather than reading prose.
- The information has a structure that prose flattens — small datasets, comparison matrices, plans with checkboxes, configurations the user will tweak.
- The user explicitly asks to *see*, *try*, or *adjust* something.

Default to prose. Don't render widgets for short factual answers, conversational replies, or content that's purely textual narrative.

## Before rendering: discover the primitives surface

Call `list_widget_examples()` to see the patterns available, then `read_widget_example(name)` for one or two that match the user's task. The example files document the `canvasAPI` capabilities and `canvas-primitives` components. Do this once per session before your first `render_widget`.

## Authoring rules

- Available globals: `React` and its hooks (`useState`, `useEffect`, `useRef`, `useMemo`, `useCallback`); `canvasAPI`; primitives from `'canvas-primitives'`.
- No `fetch`. No CDN imports. No dynamic `import()`. The card runs in a sandboxed iframe — the network surface is `canvasAPI` only.
- Declare every capability you intend to use in the `capabilities` array passed to `render_widget`. Calling an undeclared capability raises a runtime error.
- Source is capped at 256 KiB. If you need more, paginate via `widget_message`.

## Lifecycle

- `render_widget` returns a `card_id` string. Store it; pass it to `widget_update`, `widget_message`, or `widget_dispose` on later turns.
- Prefer `widget_update` over disposing and re-rendering when fixing a bug or improving a design — it preserves position and feels less jarring.
- Use `widget_message` for incremental data updates the card can absorb without remount.
- Dispose explicitly when the task that motivated the card is done — don't leak cards across topics.
- If `widget_update` or `widget_dispose` returns `card_gone: true` or `already_disposed: true`, the user closed the card. Treat that as user signal, not error.
```

- [ ] **Step 2: Create the examples directory placeholder**

```
touch assets/widget_prompts/examples/.gitkeep
```

- [ ] **Step 3: Commit**

```
git add assets/widget_prompts/
git commit -m "feat(prompts): add widget-author addendum and examples directory"
```

---

## Task 6: Load `WIDGET_AUTHOR_GUIDANCE` and append it conditionally

**Files:**
- Modify: `agent/prompt_builder.py` (alongside `MEMORY_GUIDANCE`).
- Modify: `run_agent.py:_build_system_prompt` (around line 4388).
- Test: `tests/agent/test_prompt_builder_widget.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/agent/test_prompt_builder_widget.py
"""WIDGET_AUTHOR_GUIDANCE loads from disk and is conditionally included."""

from agent import prompt_builder


def test_guidance_constant_is_non_empty():
    assert isinstance(prompt_builder.WIDGET_AUTHOR_GUIDANCE, str)
    text = prompt_builder.WIDGET_AUTHOR_GUIDANCE
    assert "render_widget" in text
    assert "widget_message" in text
    assert "widget_dispose" in text


def test_guidance_is_short_enough():
    # Lean addendum: ~30 lines target; allow up to 60 lines for slack.
    assert prompt_builder.WIDGET_AUTHOR_GUIDANCE.count("\n") < 60


def test_guidance_does_not_inline_primitives_types():
    # The addendum tells the agent to fetch examples on demand; it should
    # NOT inline the full canvasAPI surface (that's the point of Gap 5
    # in the source spec).
    text = prompt_builder.WIDGET_AUTHOR_GUIDANCE
    assert "interface CanvasAPI" not in text
    assert "type CanvasPrimitive" not in text
```

Add to a new file `tests/agent/test_build_system_prompt_widget.py` (or extend an existing prompt-builder test file):

```python
# tests/agent/test_build_system_prompt_widget.py
from unittest.mock import patch

from agent import prompt_builder
from run_agent import AIAgent


def _make_agent_with_tools(tool_names):
    agent = AIAgent.__new__(AIAgent)
    agent.skip_context_files = True
    agent.valid_tool_names = set(tool_names)
    agent._memory_store = None
    agent._memory_manager = None
    agent._memory_enabled = False
    agent._user_profile_enabled = False
    agent.model = "gpt-4o"
    agent._tool_use_enforcement = "auto"
    agent._cached_system_prompt = None
    agent.ephemeral_system_prompt = None
    return agent


def test_widget_guidance_appended_when_render_widget_present():
    agent = _make_agent_with_tools({"render_widget"})
    with patch("run_agent.load_soul_md", return_value=""):
        prompt = agent._build_system_prompt()
    assert prompt_builder.WIDGET_AUTHOR_GUIDANCE in prompt


def test_widget_guidance_absent_when_render_widget_missing():
    agent = _make_agent_with_tools({"memory"})
    with patch("run_agent.load_soul_md", return_value=""):
        prompt = agent._build_system_prompt()
    assert prompt_builder.WIDGET_AUTHOR_GUIDANCE not in prompt
```

- [ ] **Step 2: Run tests to verify they fail**

```
scripts/run_tests.sh tests/agent/test_prompt_builder_widget.py tests/agent/test_build_system_prompt_widget.py -v
```

Expected: FAIL — `WIDGET_AUTHOR_GUIDANCE` not defined; conditional append not in `_build_system_prompt`.

- [ ] **Step 3: Add the constant**

In `agent/prompt_builder.py`, after the existing guidance constants (e.g. after `SKILLS_GUIDANCE` around line 177), add:

```python
# Widget runtime — lean addendum loaded once at process import. Conditionally
# appended in run_agent._build_system_prompt when render_widget is in
# valid_tool_names. The heavy content (canvasAPI types, example .tsx files)
# stays out of the prompt; the agent fetches it on demand via the
# read_widget_example tool.
_WIDGET_PROMPT_PATH = Path(__file__).resolve().parent.parent / "assets" / "widget_prompts" / "addendum.md"
try:
    WIDGET_AUTHOR_GUIDANCE = _WIDGET_PROMPT_PATH.read_text(encoding="utf-8").strip()
except FileNotFoundError:
    WIDGET_AUTHOR_GUIDANCE = ""
```

If `Path` is not already imported at the top of `prompt_builder.py`, add `from pathlib import Path`.

- [ ] **Step 4: Append it conditionally in `_build_system_prompt`**

In `run_agent.py`, around line 4388 where `MEMORY_GUIDANCE` and friends are appended, add:

```python
# After SKILLS_GUIDANCE / MEMORY_GUIDANCE / SESSION_SEARCH_GUIDANCE block:
if "render_widget" in self.valid_tool_names:
    from agent.prompt_builder import WIDGET_AUTHOR_GUIDANCE
    if WIDGET_AUTHOR_GUIDANCE:
        prompt_parts.append(WIDGET_AUTHOR_GUIDANCE)
```

(If the existing imports at the top of `run_agent.py` already pull `WIDGET_AUTHOR_GUIDANCE` alongside `MEMORY_GUIDANCE`, prefer that — keeps it consistent with the existing import style.)

- [ ] **Step 5: Run tests to verify they pass**

```
scripts/run_tests.sh tests/agent/test_prompt_builder_widget.py tests/agent/test_build_system_prompt_widget.py -v
```

Expected: 5 passed.

- [ ] **Step 6: Run the full agent test suite to catch regressions**

```
scripts/run_tests.sh tests/agent/ tests/run_agent/ -v
```

Expected: no regressions.

- [ ] **Step 7: Commit**

```
git add agent/prompt_builder.py run_agent.py tests/agent/test_prompt_builder_widget.py tests/agent/test_build_system_prompt_widget.py
git commit -m "feat(agent): conditionally append widget-author guidance to system prompt"
```

---

## Task 7: End-to-end capability bundle smoke test

**Files:**
- Test: `tests/tui_gateway/test_widget_capability_bundle_e2e.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/tui_gateway/test_widget_capability_bundle_e2e.py
"""End-to-end: client.hello → session.create → AIAgent sees widget tools iff cap advertised.

Cross-machine alignment: the six widget tools must register together as a
bundle conditional on widget.render. Half-and-half is a wire break.
"""

import types
from unittest.mock import patch

from gateway.platforms.desktop_app import _register_client_hello
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

        agent = AIAgent.__new__(AIAgent)
        agent.valid_tool_names = set()
        # Mimic AIAgent constructing tool list with current contextvars in scope.
        from model_tools import get_tool_definitions
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
```

- [ ] **Step 2: Run the test**

```
scripts/run_tests.sh tests/tui_gateway/test_widget_capability_bundle_e2e.py -v
```

Expected: PASS — Tasks 1–6 already wired the path; this is a regression-guard.

If it fails, the bug is somewhere in Tasks 1–4. Fix; don't commit a passing test by skipping a real failure.

- [ ] **Step 3: Commit**

```
git add tests/tui_gateway/test_widget_capability_bundle_e2e.py
git commit -m "test(tui_gateway): widget cap bundle is atomic across client.hello → agent"
```

---

## Acceptance for Plan 01

- `widget.render` appears in `client.hello` server-advertised capabilities.
- A connecting client's capabilities are recorded onto the WSTransport in `_register_client_hello`.
- `session.create`, `session.resume`, `session.branch` capture caps from the transport into the session dict and bind `_WIDGET_RENDER_AVAILABLE` around `_make_agent`.
- The six widget tools register at module import with `toolset="widget"`; their `check_fn` returns `is_widget_render_available()`.
- `model_tools.get_tool_definitions(["widget"])` returns the six schemas iff `widget.render` is in the current session's capabilities, otherwise none.
- Stubs return `{"error": "not_implemented", "tool": "<name>"}`.
- `WIDGET_AUTHOR_GUIDANCE` is loaded once at import from `assets/widget_prompts/addendum.md`.
- `_build_system_prompt` appends it iff `"render_widget" in self.valid_tool_names`.
- All tests added in this plan pass via `scripts/run_tests.sh`.

Plans 02 onward replace the stubs in order: 02 fills `render_widget`/`widget_update`/`widget_message`/`widget_dispose`; 03 fills the `widget.api_call` server side; 04 closes cancellation; 05 fills the example tools.
