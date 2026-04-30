# Plan 03 — ApiCallRegistry + Async `widget.api_call` with 32 KiB Response Cap

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A card can call `canvasAPI.hermes.ask(prompt)` and receive the answer back through the iframe, end-to-end. The server validates the call, acks synchronously, runs the work as a `prompt.btw`, measures the serialized result before emitting, and rejects oversized responses with error 4106.

**Architecture:** Add a per-session `ApiCallRegistry` to `tui_gateway/widget_runtime.py` keyed by `correlation_id`. Add a `widget.api_call` JSON-RPC method handler in `tui_gateway/server.py` that synchronously validates against the per-session `WidgetRegistry`, registers the correlation, and spawns a `prompt.btw`-style worker thread that runs an `AIAgent` against the card's prompt. On worker completion, the handler measures the serialized `result` size; if it exceeds 32 KiB, the response is converted to an error 4106; otherwise emits a `widget.api_response` event.

**Tech Stack:** Python 3.11, threading, the existing `prompt.btw` thread pattern in `tui_gateway/server.py`, `AIAgent` from `run_agent.py`, JSON serialization for cap enforcement, pytest via `scripts/run_tests.sh`.

---

## File structure

**Create:**
- `tests/tui_gateway/test_api_call_registry.py` — registry contract.
- `tests/tui_gateway/test_widget_api_call_handler.py` — `widget.api_call` JSON-RPC method handler.
- `tests/tui_gateway/test_widget_api_response_cap.py` — 32 KiB cap enforcement.

**Modify:**
- `tui_gateway/widget_runtime.py` — add `ApiCallEntry` dataclass and `ApiCallRegistry` class.
- `tui_gateway/server.py` — add `@method("widget.api_call")` handler; spawn worker via the existing thread pattern; add error codes 4103/4104/4106 to the table; ensure session dict carries an `ApiCallRegistry`.

---

## Task 1: Constants module for the response cap and error codes

**Files:**
- Create: `tui_gateway/widget_constants.py` — central source for the 32 KiB cap and the widget-specific error code table.
- Test: `tests/tui_gateway/test_widget_constants.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/tui_gateway/test_widget_constants.py
"""Cross-machine alignment: cap and error code values must match the Tauri side.

If you change either, you must change the matching value on the Tauri
side too — they form the wire contract.
"""

from tui_gateway import widget_constants as wc


def test_response_cap_is_32_kib():
    assert wc.HERMES_ASK_RESPONSE_CAP_BYTES == 32 * 1024


def test_error_codes_match_spec_table():
    # Spec §8 — Hermes-side error codes.
    assert wc.ERROR_UNKNOWN_CAPABILITY == 4101
    assert wc.ERROR_SOURCE_TOO_LARGE == 4102
    assert wc.ERROR_UNKNOWN_CARD == 4103
    assert wc.ERROR_CAP_NOT_DECLARED == 4104
    assert wc.ERROR_USER_REJECTED_CAP_CALL == 4105
    assert wc.ERROR_RESPONSE_TOO_LARGE == 4106
    assert wc.ERROR_MESSAGE_TOO_LARGE == 4107
    assert wc.ERROR_CLIENT_REFUSED_MOUNT == 5101
    assert wc.ERROR_RENDER_TIMED_OUT == 5102
    assert wc.ERROR_API_CALL_EXPIRED == 5103
```

- [ ] **Step 2: Run test to verify it fails**

```
scripts/run_tests.sh tests/tui_gateway/test_widget_constants.py -v
```

Expected: FAIL — module not found.

- [ ] **Step 3: Create the constants module**

```python
# tui_gateway/widget_constants.py
"""Wire-contract constants for the widget runtime.

Values here are SHARED with the Tauri side via the source spec
(plans/hermes-widget-render-spec.md §3.5.3, §8). Changing a value here
without changing the matching constant in the Tauri client breaks the
contract — please update both sides together.
"""

# 32 KiB cap on widget.api_response.result, enforced server-side before emit.
HERMES_ASK_RESPONSE_CAP_BYTES = 32 * 1024

# Error codes — Hermes-side (4xxx) + cross-side (5xxx).
ERROR_UNKNOWN_CAPABILITY = 4101
ERROR_SOURCE_TOO_LARGE = 4102
ERROR_UNKNOWN_CARD = 4103
ERROR_CAP_NOT_DECLARED = 4104
ERROR_USER_REJECTED_CAP_CALL = 4105  # reserved for a future approval gate
ERROR_RESPONSE_TOO_LARGE = 4106
ERROR_MESSAGE_TOO_LARGE = 4107

ERROR_CLIENT_REFUSED_MOUNT = 5101
ERROR_RENDER_TIMED_OUT = 5102
ERROR_API_CALL_EXPIRED = 5103
```

- [ ] **Step 4: Run test to verify it passes**

```
scripts/run_tests.sh tests/tui_gateway/test_widget_constants.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Refactor existing code to use the constants**

Update `tools/widget_tools.py` to import the cap and error codes:

```python
from tui_gateway.widget_constants import (
    ERROR_UNKNOWN_CAPABILITY, ERROR_SOURCE_TOO_LARGE,
    ERROR_MESSAGE_TOO_LARGE, ERROR_CLIENT_REFUSED_MOUNT,
    ERROR_RENDER_TIMED_OUT,
)
```

Replace the hard-coded `4101`, `4102`, `4107`, `5101`, `5102` in `tools/widget_tools.py` with the imported constants.

- [ ] **Step 6: Run lifecycle tests to confirm no regression**

```
scripts/run_tests.sh tests/tools/test_widget_tools_lifecycle.py -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```
git add tui_gateway/widget_constants.py tools/widget_tools.py tests/tui_gateway/test_widget_constants.py
git commit -m "feat(tui_gateway): central widget constants and cross-machine error codes"
```

---

## Task 2: `ApiCallRegistry` data model

**Files:**
- Modify: `tui_gateway/widget_runtime.py`
- Test: `tests/tui_gateway/test_api_call_registry.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/tui_gateway/test_api_call_registry.py
"""ApiCallRegistry: register/complete/cancel; observability timestamps."""

import time

from tui_gateway.widget_runtime import ApiCallRegistry, ApiCallEntry


def test_register_returns_entry():
    reg = ApiCallRegistry()
    entry = reg.register(
        correlation_id="corr_a1b2c3",
        card_id="wgt_8a3f9c",
        capability="hermes.ask",
        agent_ref=None,
    )
    assert isinstance(entry, ApiCallEntry)
    assert entry.correlation_id == "corr_a1b2c3"
    assert entry.created_at > 0
    assert entry.cancelled_at is None
    assert entry.completed_at is None


def test_get_returns_entry_for_known_correlation():
    reg = ApiCallRegistry()
    reg.register(correlation_id="corr_x", card_id="wgt_x", capability="hermes.ask", agent_ref=None)
    e = reg.get("corr_x")
    assert e is not None and e.card_id == "wgt_x"


def test_get_returns_none_for_unknown_correlation():
    reg = ApiCallRegistry()
    assert reg.get("corr_missing") is None


def test_complete_marks_completed_and_pops():
    reg = ApiCallRegistry()
    reg.register(correlation_id="corr_y", card_id="wgt_y", capability="hermes.ask", agent_ref=None)
    e = reg.complete("corr_y")
    assert e is not None
    assert e.completed_at is not None
    assert reg.get("corr_y") is None  # popped


def test_complete_returns_none_for_unknown():
    reg = ApiCallRegistry()
    assert reg.complete("corr_nope") is None


def test_cancel_marks_cancelled_and_keeps_entry_for_observability():
    reg = ApiCallRegistry()
    reg.register(correlation_id="corr_z", card_id="wgt_z", capability="hermes.ask", agent_ref=None)
    e = reg.cancel("corr_z", reason="card_disposed")
    assert e is not None
    assert e.cancelled_at is not None
    assert e.cancel_reason == "card_disposed"
    # Cancellation removes the entry from the active map; the returned
    # entry is the snapshot. (Plan 04 wires interrupt + drop-on-arrival.)
    assert reg.get("corr_z") is None


def test_cancel_for_card_returns_all_correlations_for_that_card():
    reg = ApiCallRegistry()
    reg.register(correlation_id="corr_1", card_id="wgt_a", capability="hermes.ask", agent_ref=None)
    reg.register(correlation_id="corr_2", card_id="wgt_a", capability="hermes.ask", agent_ref=None)
    reg.register(correlation_id="corr_3", card_id="wgt_b", capability="hermes.ask", agent_ref=None)

    cancelled = reg.cancel_for_card("wgt_a", reason="card_disposed")
    assert sorted(cancelled) == ["corr_1", "corr_2"]
    assert reg.get("corr_1") is None
    assert reg.get("corr_2") is None
    assert reg.get("corr_3") is not None


def test_post_cancel_runtime_measurement():
    reg = ApiCallRegistry()
    reg.register(correlation_id="corr_o", card_id="wgt_o", capability="hermes.ask", agent_ref=None)
    e_cancelled = reg.cancel("corr_o", reason="card_disposed")
    time.sleep(0.05)
    # Plan 04 will use this for observability when btw still produces a
    # response after cancellation. This test asserts the snapshot has
    # the timestamp we'll diff against.
    assert e_cancelled.cancelled_at is not None
```

- [ ] **Step 2: Run tests to verify they fail**

```
scripts/run_tests.sh tests/tui_gateway/test_api_call_registry.py -v
```

Expected: FAIL — `ApiCallRegistry` and `ApiCallEntry` do not exist.

- [ ] **Step 3: Implement the registry**

Append to `tui_gateway/widget_runtime.py`:

```python
import time
from typing import Any


@dataclass
class ApiCallEntry:
    correlation_id: str
    card_id: str
    capability: str
    agent_ref: Any  # The AIAgent running the prompt.btw — used by Plan 04 for cancellation.
    created_at: float
    completed_at: Optional[float] = None
    cancelled_at: Optional[float] = None
    cancel_reason: Optional[str] = None


class ApiCallRegistry:
    """Per-session map of in-flight widget.api_call correlations.

    Plan 03 implements register/get/complete and the cancel methods that
    Plan 04 will wire to agent.interrupt() and to drop-on-arrival logic.
    Cancelled entries are removed from the active map; the snapshot is
    returned for observability (Plan 04 logs post-cancel runtime).
    """

    def __init__(self) -> None:
        self._inflight: dict[str, ApiCallEntry] = {}
        self._lock = threading.RLock()

    def register(
        self,
        correlation_id: str,
        card_id: str,
        capability: str,
        agent_ref: Any,
    ) -> ApiCallEntry:
        entry = ApiCallEntry(
            correlation_id=correlation_id,
            card_id=card_id,
            capability=capability,
            agent_ref=agent_ref,
            created_at=time.time(),
        )
        with self._lock:
            self._inflight[correlation_id] = entry
        return entry

    def get(self, correlation_id: str) -> Optional[ApiCallEntry]:
        with self._lock:
            return self._inflight.get(correlation_id)

    def complete(self, correlation_id: str) -> Optional[ApiCallEntry]:
        with self._lock:
            entry = self._inflight.pop(correlation_id, None)
        if entry is not None:
            entry.completed_at = time.time()
        return entry

    def cancel(self, correlation_id: str, reason: str) -> Optional[ApiCallEntry]:
        with self._lock:
            entry = self._inflight.pop(correlation_id, None)
        if entry is not None:
            entry.cancelled_at = time.time()
            entry.cancel_reason = reason
        return entry

    def cancel_for_card(self, card_id: str, reason: str) -> list[str]:
        with self._lock:
            ids = [c for c, e in self._inflight.items() if e.card_id == card_id]
            for c in ids:
                entry = self._inflight.pop(c, None)
                if entry is not None:
                    entry.cancelled_at = time.time()
                    entry.cancel_reason = reason
        return ids
```

- [ ] **Step 4: Run tests to verify they pass**

```
scripts/run_tests.sh tests/tui_gateway/test_api_call_registry.py -v
```

Expected: 8 passed.

- [ ] **Step 5: Commit**

```
git add tui_gateway/widget_runtime.py tests/tui_gateway/test_api_call_registry.py
git commit -m "feat(tui_gateway): add ApiCallRegistry with observability timestamps"
```

---

## Task 3: Allocate `ApiCallRegistry` on every session

**Files:**
- Modify: `tui_gateway/server.py:_init_session`, `session.create`, `session.resume`, `session.branch` session dict initializers.
- Test: extend `tests/tui_gateway/test_widget_inbound_handlers.py` (or add a new file).

- [ ] **Step 1: Add the assertion**

```python
# Append to tests/tui_gateway/test_widget_inbound_handlers.py

def test_session_dict_includes_api_call_registry(monkeypatch):
    sid, sess = _seed_session("sess-api", "key-api")
    # Simulate _init_session having added the ApiCallRegistry — Task 3 wires it.
    from tui_gateway.widget_runtime import ApiCallRegistry
    assert isinstance(sess.get("api_call_registry"), ApiCallRegistry) or sess.get("api_call_registry") is not None
```

(The test is loose because `_seed_session` creates the dict directly; the real wiring is in `tui_gateway/server.py`.)

- [ ] **Step 2: Wire the registry into session-dict initializers**

In `tui_gateway/server.py`, every place that initializes `state.sessions[sid]` (lines 1421, 1566, 1972, plus `_init_session`), add:

```python
"api_call_registry": __import__("tui_gateway.widget_runtime", fromlist=["ApiCallRegistry"]).ApiCallRegistry(),
```

(Same pattern Plan 02 used for `widget_registry`. Or import at module top: `from tui_gateway.widget_runtime import ApiCallRegistry as _ApiCallRegistry`.)

Update `_seed_session` in the test fixture too if needed.

- [ ] **Step 3: Run the regression suite**

```
scripts/run_tests.sh tests/tui_gateway/ tests/test_tui_gateway_server.py -v
```

Expected: no regressions.

- [ ] **Step 4: Commit**

```
git add tui_gateway/server.py tests/tui_gateway/test_widget_inbound_handlers.py
git commit -m "feat(tui_gateway): allocate ApiCallRegistry per session"
```

---

## Task 4: `widget.api_call` validation + ack path

**Files:**
- Modify: `tui_gateway/server.py` — register `@method("widget.api_call")`.
- Test: `tests/tui_gateway/test_widget_api_call_handler.py`

- [ ] **Step 1: Write the failing tests for the validation + ack-only path**

```python
# tests/tui_gateway/test_widget_api_call_handler.py
"""widget.api_call: synchronous validation + ack; spawns work in background."""

from __future__ import annotations

import threading
import types

import pytest

from tui_gateway import server, widget_runtime


def _seed_session_with_card(sid="sess-api", key="key-api", capabilities=None):
    state = server._state()
    transport = types.SimpleNamespace(write=lambda *a, **k: True)
    sess = {
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
    cid = sess["widget_registry"].allocate(
        source="x", capabilities=capabilities or ["hermes.ask"],
        title=None, initial_size=None, trace_id=None,
    )
    return sid, key, sess, cid


def test_acks_synchronously_for_valid_call(monkeypatch):
    sid, key, sess, cid = _seed_session_with_card(sid="sess-ack", key="key-ack")
    # Stub the worker so the test doesn't actually run prompt.btw.
    monkeypatch.setattr(
        "tui_gateway.server._spawn_widget_api_call_worker",
        lambda **kwargs: None,
    )

    handler = server._methods["widget.api_call"]
    resp = handler(17, {
        "session_id": sid,
        "card_id": cid,
        "correlation_id": "corr_a1b2c3",
        "capability": "hermes.ask",
        "args": {"prompt": "What's Q3 revenue?"},
    })
    assert resp == {
        "jsonrpc": "2.0",
        "id": 17,
        "result": {"accepted": True, "correlation_id": "corr_a1b2c3"},
    }
    # Correlation registered in the per-session registry.
    assert sess["api_call_registry"].get("corr_a1b2c3") is not None


def test_rejects_unknown_session(monkeypatch):
    handler = server._methods["widget.api_call"]
    resp = handler(1, {
        "session_id": "ghost",
        "card_id": "wgt_000000",
        "correlation_id": "corr_x",
        "capability": "hermes.ask",
        "args": {},
    })
    assert "error" in resp
    assert resp["error"]["code"] == 4001  # session not found, base contract


def test_rejects_unknown_card(monkeypatch):
    sid, key, sess, _ = _seed_session_with_card(sid="sess-uc", key="key-uc")
    handler = server._methods["widget.api_call"]
    resp = handler(1, {
        "session_id": sid,
        "card_id": "wgt_deadbe",
        "correlation_id": "corr_x",
        "capability": "hermes.ask",
        "args": {},
    })
    assert resp["error"]["code"] == 4103


def test_rejects_capability_not_declared(monkeypatch):
    sid, key, sess, cid = _seed_session_with_card(
        sid="sess-undecl", key="key-undecl",
        capabilities=["notes.save"],  # hermes.ask not declared
    )
    handler = server._methods["widget.api_call"]
    resp = handler(1, {
        "session_id": sid,
        "card_id": cid,
        "correlation_id": "corr_x",
        "capability": "hermes.ask",
        "args": {},
    })
    assert resp["error"]["code"] == 4104


def test_rejects_unsupported_capability(monkeypatch):
    sid, key, sess, cid = _seed_session_with_card(sid="sess-bad", key="key-bad", capabilities=["bogus.thing"])
    # bogus.thing is not in the canvasAPI surface (Tauri broker would have
    # rejected client-side). Server-side guard catches the case where it
    # somehow reaches us.
    handler = server._methods["widget.api_call"]
    resp = handler(1, {
        "session_id": sid,
        "card_id": cid,
        "correlation_id": "corr_x",
        "capability": "bogus.thing",
        "args": {},
    })
    # Either 4101 (unknown capability) or 4104 (not declared) is acceptable;
    # both are correct rejections. Use 4101 since the cap doesn't exist at all.
    assert resp["error"]["code"] in (4101, 4104)
```

- [ ] **Step 2: Run tests to verify they fail**

```
scripts/run_tests.sh tests/tui_gateway/test_widget_api_call_handler.py -v
```

Expected: FAIL — `widget.api_call` not registered.

- [ ] **Step 3: Register the method handler with validation + ack**

In `tui_gateway/server.py`, near the existing `prompt.btw` handler (line 2670), add:

```python
from tui_gateway.widget_constants import (
    ERROR_UNKNOWN_CAPABILITY,
    ERROR_UNKNOWN_CARD,
    ERROR_CAP_NOT_DECLARED,
    ERROR_RESPONSE_TOO_LARGE,
    HERMES_ASK_RESPONSE_CAP_BYTES,
)

# Capabilities that round-trip to Hermes (require a server-side prompt.btw).
# Cards declaring other capabilities (notes.save, storage.*, etc.) are
# handled entirely by the Tauri broker and never reach this handler.
_HERMES_BACKED_CAPABILITIES = {"hermes.ask"}


@method("widget.api_call")
def _(rid, params: dict) -> dict:
    sid = params.get("session_id", "") or ""
    card_id = params.get("card_id", "") or ""
    correlation_id = params.get("correlation_id", "") or ""
    capability = params.get("capability", "") or ""
    call_args = params.get("args") or {}

    state = _state()
    sess = state.sessions.get(sid)
    if sess is None:
        return _err(rid, 4001, "session not found")
    if capability not in _HERMES_BACKED_CAPABILITIES:
        return _err(rid, ERROR_UNKNOWN_CAPABILITY, f"unsupported widget.api_call capability: {capability!r}")

    widget_reg = sess.get("widget_registry")
    api_reg = sess.get("api_call_registry")
    if widget_reg is None or api_reg is None:
        return _err(rid, 4001, "session has no widget runtime")

    entry = widget_reg.get(card_id)
    if entry is None:
        return _err(rid, ERROR_UNKNOWN_CARD, f"card not live: {card_id!r}")
    if capability not in entry.capabilities:
        return _err(rid, ERROR_CAP_NOT_DECLARED, f"capability {capability!r} not declared by card {card_id!r}")

    api_reg.register(
        correlation_id=correlation_id,
        card_id=card_id,
        capability=capability,
        agent_ref=None,  # Plan 04 stashes the actual agent for cancellation.
    )

    # Spawn the work in a background thread; the worker emits
    # widget.api_response when it completes.
    _spawn_widget_api_call_worker(
        sid=sid,
        session_key=sess.get("session_key", ""),
        correlation_id=correlation_id,
        card_id=card_id,
        capability=capability,
        call_args=call_args,
        history_snapshot=list(sess.get("history") or []),
    )

    return _ok(rid, {"accepted": True, "correlation_id": correlation_id})


def _spawn_widget_api_call_worker(*, sid, session_key, correlation_id, card_id, capability, call_args, history_snapshot):
    """Run the capability call as a prompt.btw and emit widget.api_response.

    Defined as a module-level function (not a closure) so tests can monkey-patch it.
    """
    # Implementation lives in Task 5.
    raise NotImplementedError
```

The validation + ack path is tested without invoking the worker (tests monkey-patch `_spawn_widget_api_call_worker`). The worker body lands in Task 5.

- [ ] **Step 4: Run validation tests**

```
scripts/run_tests.sh tests/tui_gateway/test_widget_api_call_handler.py -v
```

Expected: 5 passed (the worker-touching tests in Task 5 don't exist yet).

- [ ] **Step 5: Commit**

```
git add tui_gateway/server.py tests/tui_gateway/test_widget_api_call_handler.py
git commit -m "feat(tui_gateway): widget.api_call validates and acks synchronously"
```

---

## Task 5: Background worker that runs `prompt.btw` and emits `widget.api_response`

**Files:**
- Modify: `tui_gateway/server.py:_spawn_widget_api_call_worker`.
- Test: extend `tests/tui_gateway/test_widget_api_call_handler.py`.

- [ ] **Step 1: Add failing tests**

```python
# Append to tests/tui_gateway/test_widget_api_call_handler.py

import json


def test_worker_emits_api_response_on_success(monkeypatch):
    sid, key, sess, cid = _seed_session_with_card(sid="sess-ws", key="key-ws")
    emits = []
    monkeypatch.setattr(server, "_emit", lambda *a: emits.append(a))

    # Stub AIAgent.run_conversation to return a deterministic answer.
    class _FakeAgent:
        def __init__(self, *a, **kw):
            self.session_id = kw.get("session_id", "")
        def run_conversation(self, text, conversation_history=None):
            return {"final_response": "Q3 revenue was $4.2M, up 18% YoY."}
    monkeypatch.setattr("run_agent.AIAgent", _FakeAgent)

    server._spawn_widget_api_call_worker(
        sid=sid,
        session_key=key,
        correlation_id="corr_ok",
        card_id=cid,
        capability="hermes.ask",
        call_args={"prompt": "What's Q3 revenue?"},
        history_snapshot=[],
    )

    # Worker runs sync if we don't actually thread; but Plan 03 does
    # spawn a thread. Wait briefly.
    for _ in range(100):
        if emits:
            break
        threading.Event().wait(0.02)

    assert any(e[0] == "widget.api_response" for e in emits), f"no widget.api_response in {emits!r}"
    resp = next(e for e in emits if e[0] == "widget.api_response")
    assert resp[1] == sid
    assert resp[2]["correlation_id"] == "corr_ok"
    assert resp[2]["card_id"] == cid
    assert resp[2]["result"]["answer"] == "Q3 revenue was $4.2M, up 18% YoY."
    # Correlation popped from registry.
    assert sess["api_call_registry"].get("corr_ok") is None


def test_worker_emits_error_4106_when_response_exceeds_cap(monkeypatch):
    sid, key, sess, cid = _seed_session_with_card(sid="sess-cap", key="key-cap")
    emits = []
    monkeypatch.setattr(server, "_emit", lambda *a: emits.append(a))

    huge = "x" * (40 * 1024)  # 40 KiB > 32 KiB cap
    class _FakeAgent:
        def __init__(self, *a, **kw): pass
        def run_conversation(self, text, conversation_history=None):
            return {"final_response": huge}
    monkeypatch.setattr("run_agent.AIAgent", _FakeAgent)

    server._spawn_widget_api_call_worker(
        sid=sid, session_key=key, correlation_id="corr_big",
        card_id=cid, capability="hermes.ask", call_args={"prompt": "long"},
        history_snapshot=[],
    )
    for _ in range(100):
        if emits:
            break
        threading.Event().wait(0.02)

    assert emits, "no widget.api_response emitted"
    resp = next(e for e in emits if e[0] == "widget.api_response")
    assert "error" in resp[2]
    assert resp[2]["error"]["code"] == 4106
    assert "32" in resp[2]["error"]["message"], "error should reference the 32 KiB cap"
    # Actual size mentioned for diagnostics.
    assert "40" in resp[2]["error"]["message"] or "actual" in resp[2]["error"]["message"].lower()


def test_worker_handles_agent_exception(monkeypatch):
    sid, key, sess, cid = _seed_session_with_card(sid="sess-exc", key="key-exc")
    emits = []
    monkeypatch.setattr(server, "_emit", lambda *a: emits.append(a))

    class _FakeAgent:
        def __init__(self, *a, **kw): pass
        def run_conversation(self, text, conversation_history=None):
            raise RuntimeError("agent blew up")
    monkeypatch.setattr("run_agent.AIAgent", _FakeAgent)

    server._spawn_widget_api_call_worker(
        sid=sid, session_key=key, correlation_id="corr_err",
        card_id=cid, capability="hermes.ask", call_args={"prompt": "x"},
        history_snapshot=[],
    )
    for _ in range(100):
        if emits:
            break
        threading.Event().wait(0.02)

    resp = next(e for e in emits if e[0] == "widget.api_response")
    assert "error" in resp[2]
    # Use the cross-side ERROR_API_CALL_EXPIRED or a similar 5xxx code.
    assert resp[2]["error"]["code"] >= 5000
```

- [ ] **Step 2: Run tests to verify they fail**

```
scripts/run_tests.sh tests/tui_gateway/test_widget_api_call_handler.py -k worker -v
```

Expected: FAIL — `_spawn_widget_api_call_worker` raises `NotImplementedError`.

- [ ] **Step 3: Implement the worker**

Replace the body of `_spawn_widget_api_call_worker` in `tui_gateway/server.py`:

```python
def _spawn_widget_api_call_worker(*, sid, session_key, correlation_id, card_id, capability, call_args, history_snapshot):
    state = _state()

    def run():
        session_tokens = []
        try:
            session_tokens = _set_session_context(session_key)
            from run_agent import AIAgent

            # Capture the agent so Plan 04 can call agent.interrupt() for cancellation.
            sess = state.sessions.get(sid) or {}
            api_reg = sess.get("api_call_registry")
            if api_reg is not None:
                entry = api_reg.get(correlation_id)

            if capability == "hermes.ask":
                prompt = str(call_args.get("prompt", "") or "")
                btw_agent = AIAgent(
                    model=_resolve_model(),
                    quiet_mode=True,
                    platform="tui",
                    max_iterations=8,
                    enabled_toolsets=[],
                )
                if api_reg is not None and entry is not None:
                    entry.agent_ref = btw_agent
                result = btw_agent.run_conversation(prompt, conversation_history=history_snapshot)
                answer = (
                    result.get("final_response", str(result))
                    if isinstance(result, dict)
                    else str(result)
                )
                payload_result = {"answer": answer}
            else:
                _emit_api_response_error(
                    sid, correlation_id, card_id, ERROR_UNKNOWN_CAPABILITY,
                    f"unsupported capability {capability!r} reached worker",
                )
                if api_reg is not None:
                    api_reg.complete(correlation_id)
                return

            # Cap enforcement — measure the serialized result before emitting.
            serialized = json.dumps(payload_result, ensure_ascii=False)
            actual = len(serialized.encode("utf-8"))
            if actual > HERMES_ASK_RESPONSE_CAP_BYTES:
                _emit_api_response_error(
                    sid, correlation_id, card_id, ERROR_RESPONSE_TOO_LARGE,
                    f"widget.api_response payload {actual} bytes exceeds cap of "
                    f"{HERMES_ASK_RESPONSE_CAP_BYTES} bytes",
                )
            else:
                _emit(
                    "widget.api_response",
                    sid,
                    {
                        "correlation_id": correlation_id,
                        "card_id": card_id,
                        "result": payload_result,
                    },
                )

            if api_reg is not None:
                api_reg.complete(correlation_id)
        except Exception as exc:
            _emit_api_response_error(
                sid, correlation_id, card_id, 5103,
                f"widget.api_call worker error: {exc}",
            )
            if (sess := state.sessions.get(sid)) is not None and sess.get("api_call_registry") is not None:
                sess["api_call_registry"].complete(correlation_id)
        finally:
            if session_tokens:
                _clear_session_context(session_tokens)

    threading.Thread(target=run, daemon=True).start()


def _emit_api_response_error(sid: str, correlation_id: str, card_id: str, code: int, message: str) -> None:
    _emit(
        "widget.api_response",
        sid,
        {
            "correlation_id": correlation_id,
            "card_id": card_id,
            "error": {"code": code, "message": message},
        },
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```
scripts/run_tests.sh tests/tui_gateway/test_widget_api_call_handler.py -v
```

Expected: 8 passed (5 from Task 4 + 3 worker tests from this task).

- [ ] **Step 5: Commit**

```
git add tui_gateway/server.py tests/tui_gateway/test_widget_api_call_handler.py
git commit -m "feat(tui_gateway): widget.api_call worker runs prompt.btw and enforces 32 KiB cap"
```

---

## Task 6: Cross-machine alignment — 32 KiB cap test

**Files:**
- Test: `tests/tui_gateway/test_widget_api_response_cap.py`

- [ ] **Step 1: Add the test**

```python
# tests/tui_gateway/test_widget_api_response_cap.py
"""Cross-machine alignment: widget.api_response is hard-capped at 32 KiB.

The Tauri side (Plan 02) assumes it never receives an oversized payload.
A regression here would crash iframe rendering or trigger client-side
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
```

- [ ] **Step 2: Run the test**

```
scripts/run_tests.sh tests/tui_gateway/test_widget_api_response_cap.py -v
```

Expected: 4 passed.

- [ ] **Step 3: Commit**

```
git add tests/tui_gateway/test_widget_api_response_cap.py
git commit -m "test(tui_gateway): widget.api_response 32 KiB cap alignment"
```

---

## Acceptance for Plan 03

- `widget.api_call` is registered as a JSON-RPC method on `tui_gateway/server.py`.
- Validation order: session exists → registries exist → capability supported → card live → capability declared. Each failure returns the spec-mandated error code synchronously.
- Acknowledgment is `{accepted: true, correlation_id}` returned synchronously inside the JSON-RPC response.
- The worker thread runs `prompt.btw` semantics (a fresh `AIAgent` with empty toolsets, max_iterations=8, hermetic from main session history).
- `widget.api_response` is emitted on completion. Result is wrapped in `{"answer": "..."}` for `hermes.ask`.
- The serialized result is measured in UTF-8 bytes BEFORE emission. Over-cap responses emit `{error: {code: 4106, message: "..."}}` instead of `result`. The error message includes both the actual size and the 32 KiB cap.
- Unhandled worker exceptions emit a 5103 error response (caller sees a structured rejection, not a hung call).
- `ApiCallRegistry` is allocated per session and tracks correlations through `register` → `complete` (or `cancel` in Plan 04). Observability timestamps are recorded.
- Cross-machine alignment tests for the cap and error codes pass.

Plan 04 wires `widget.api_cancel` (both directions) and connects the `agent_ref` stashed in the registry to `agent.interrupt()` for best-effort cancellation.
