# Plan 04 — `widget.api_cancel` Both Directions, Drop-on-Arrival, Lite Observability

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A pending `widget.api_call` can be cancelled cleanly, in either direction. Closing a card mid-`hermes.ask` calls `agent.interrupt()` and prevents the eventual response from being emitted to a now-gone iframe. Card disposal triggers cancellation of every associated correlation. Cancellation latency and "btw kept running after cancel" duration are recorded for observability.

**Architecture:** Add an inbound `widget.api_cancel` event handler that looks up the correlation in the per-session `ApiCallRegistry`, calls `agent.interrupt()` on the stashed `agent_ref` (best-effort), and removes the entry. Add a "cancelled" check in the `_spawn_widget_api_call_worker` completion path so a btw that finishes after cancellation drops its result instead of emitting. Wire `widget.dispose` (from Plan 02) to also iterate the `ApiCallRegistry` and cancel + emit outbound `widget.api_cancel` for each correlation tied to that card. Same for session teardown.

**Tech Stack:** Python 3.11, `tools/interrupt.py` (per-thread interrupt signaling — already exists), the `_event_handlers` map and dispatch path from Plan 02, the `ApiCallRegistry` from Plan 03, pytest via `scripts/run_tests.sh`.

---

## File structure

**Create:**
- `tests/tui_gateway/test_widget_api_cancel_inbound.py` — inbound `widget.api_cancel` handler.
- `tests/tui_gateway/test_widget_api_cancel_card_disposal.py` — card disposal triggers cancellation + outbound emit.
- `tests/tui_gateway/test_widget_api_cancel_drop_on_arrival.py` — late-arriving btw result is dropped after cancel.
- `tests/tui_gateway/test_widget_api_cancel_envelope_alignment.py` — cross-machine envelope shape.

**Modify:**
- `tui_gateway/widget_runtime.py` — register the inbound `widget.api_cancel` event handler; expose a helper that the dispose handler can call.
- `tui_gateway/server.py` — modify the worker completion path to skip emit when the correlation has been cancelled; modify `widget.dispose` handler in `tools/widget_tools.py` (and inbound `widget.disposed` handler) to cancel any associated correlations and emit outbound `widget.api_cancel`.
- `tools/widget_tools.py:_widget_dispose` — call into the api-call cancel helper before emitting `widget.dispose`.

---

## Task 1: Inbound `widget.api_cancel` handler

**Files:**
- Modify: `tui_gateway/widget_runtime.py:_register_inbound_event_handlers`.
- Test: `tests/tui_gateway/test_widget_api_cancel_inbound.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/tui_gateway/test_widget_api_cancel_inbound.py
"""Inbound widget.api_cancel: lookup correlation, call agent.interrupt(), pop from registry."""

import threading
import types

from tui_gateway import server, widget_runtime


def _seed(sid="sess-cx", key="key-cx"):
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
    return sid, state.sessions[sid]


def test_api_cancel_calls_interrupt_on_agent_ref():
    sid, sess = _seed("sess-cx-int", "key-cx-int")
    interrupted = {"called": False, "msg": None}

    class _FakeAgent:
        def interrupt(self, message=None):
            interrupted["called"] = True
            interrupted["msg"] = message

    api_reg = sess["api_call_registry"]
    cid = sess["widget_registry"].allocate(source="x", capabilities=["hermes.ask"], title=None, initial_size=None, trace_id=None)
    api_reg.register(correlation_id="corr_cx", card_id=cid, capability="hermes.ask", agent_ref=_FakeAgent())

    server.dispatch({
        "jsonrpc": "2.0",
        "method": "event",
        "params": {
            "type": "widget.api_cancel",
            "session_id": sid,
            "payload": {"correlation_id": "corr_cx", "card_id": cid, "reason": "card_disposed"},
        },
    })

    assert interrupted["called"] is True
    # Correlation removed from active map.
    assert api_reg.get("corr_cx") is None


def test_api_cancel_for_unknown_correlation_is_silent():
    sid, sess = _seed("sess-cx-unk", "key-cx-unk")
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "event",
        "params": {
            "type": "widget.api_cancel",
            "session_id": sid,
            "payload": {"correlation_id": "corr_ghost", "card_id": "wgt_x", "reason": "user_cancelled"},
        },
    })
    # No assertion — just must not raise.


def test_api_cancel_records_reason_for_observability():
    sid, sess = _seed("sess-cx-reas", "key-cx-reas")
    api_reg = sess["api_call_registry"]
    cid = sess["widget_registry"].allocate(source="x", capabilities=["hermes.ask"], title=None, initial_size=None, trace_id=None)
    entry = api_reg.register(correlation_id="corr_obs", card_id=cid, capability="hermes.ask", agent_ref=None)

    server.dispatch({
        "jsonrpc": "2.0",
        "method": "event",
        "params": {
            "type": "widget.api_cancel",
            "session_id": sid,
            "payload": {"correlation_id": "corr_obs", "card_id": cid, "reason": "card_updated"},
        },
    })

    # Entry was popped — but reason was recorded on the snapshot.
    assert entry.cancel_reason == "card_updated"
    assert entry.cancelled_at is not None
```

- [ ] **Step 2: Run tests to verify they fail**

```
scripts/run_tests.sh tests/tui_gateway/test_widget_api_cancel_inbound.py -v
```

Expected: FAIL — handler not registered.

- [ ] **Step 3: Register the handler**

Extend `_register_inbound_event_handlers` in `tui_gateway/widget_runtime.py`:

```python
@event_handler("widget.api_cancel")
def _on_api_cancel(params: dict) -> None:
    sid = params.get("session_id", "")
    payload = params.get("payload") or {}
    correlation_id = str(payload.get("correlation_id", "") or "")
    reason = str(payload.get("reason", "user_cancelled") or "user_cancelled")

    state = _state_for_session_safe(sid)
    sess = (state or {}).get(sid) if state else None
    if not sess:
        return
    api_reg = sess.get("api_call_registry")
    if api_reg is None:
        return

    entry = api_reg.cancel(correlation_id, reason=reason)
    if entry is not None and entry.agent_ref is not None:
        try:
            entry.agent_ref.interrupt()
        except Exception:
            # Best-effort. Worker continues; drop-on-arrival
            # in Task 3 catches it.
            pass


def _state_for_session_safe(sid: str):
    """Return the sessions dict for a session id, or None if not registered."""
    from tui_gateway.server import _state_for_session
    if not sid:
        return None
    state = _state_for_session(sid)
    return state.sessions if state is not None else None
```

(`_state_for_session_safe` is a thin wrapper that returns the `sessions` dict directly so the inbound handler reads `(state or {}).get(sid)` cleanly.)

- [ ] **Step 4: Run tests to verify they pass**

```
scripts/run_tests.sh tests/tui_gateway/test_widget_api_cancel_inbound.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```
git add tui_gateway/widget_runtime.py tests/tui_gateway/test_widget_api_cancel_inbound.py
git commit -m "feat(tui_gateway): inbound widget.api_cancel calls agent.interrupt() and pops correlation"
```

---

## Task 2: Card disposal cancels associated correlations + emits outbound `widget.api_cancel`

**Files:**
- Modify: `tools/widget_tools.py:_widget_dispose` — before emitting `widget.dispose`, cancel related correlations and emit `widget.api_cancel` for each.
- Modify: `tui_gateway/widget_runtime.py` — extend the `widget.disposed` inbound handler (Plan 02) to also cancel correlations.
- Test: `tests/tui_gateway/test_widget_api_cancel_card_disposal.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/tui_gateway/test_widget_api_cancel_card_disposal.py
"""Card disposal cancels every correlation associated with that card.

Server-initiated dispose (widget_dispose tool): emit widget.dispose AND
emit widget.api_cancel for each in-flight correlation, AND remove from registry.

Client-initiated disposal (widget.disposed inbound event): same registry
clear, but no outbound widget.api_cancel — the client already knows.
"""

import json
import threading
import types

import pytest

from tools.registry import registry
from tui_gateway import server, widget_runtime


def _setup(sid="sess-disp-cx", key="key-disp-cx"):
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
    sess = state.sessions[sid]
    cid = sess["widget_registry"].allocate(source="x", capabilities=["hermes.ask"], title=None, initial_size=None, trace_id=None)
    return sid, key, sess, cid


def test_widget_dispose_tool_cancels_inflight_calls(monkeypatch):
    sid, key, sess, cid = _setup("sess-tool-disp", "key-tool-disp")
    emits = []
    monkeypatch.setattr(server, "_emit", lambda *a: emits.append(a))

    interrupted = []
    class _FakeAgent:
        def __init__(self, name): self.name = name
        def interrupt(self, message=None):
            interrupted.append(self.name)

    api_reg = sess["api_call_registry"]
    api_reg.register(correlation_id="corr_a", card_id=cid, capability="hermes.ask", agent_ref=_FakeAgent("a"))
    api_reg.register(correlation_id="corr_b", card_id=cid, capability="hermes.ask", agent_ref=_FakeAgent("b"))
    other_cid = sess["widget_registry"].allocate(source="y", capabilities=["hermes.ask"], title=None, initial_size=None, trace_id=None)
    api_reg.register(correlation_id="corr_c", card_id=other_cid, capability="hermes.ask", agent_ref=_FakeAgent("c"))

    handler = registry.get_entry("widget_dispose").handler
    result = json.loads(handler({"card_id": cid, "reason": "task_complete"}, session_id=key))
    assert result["disposed"] is True

    # widget.dispose emitted once for the card.
    assert any(e[0] == "widget.dispose" and e[2]["card_id"] == cid for e in emits)
    # widget.api_cancel emitted for each correlation tied to the disposed card.
    cancels = [e for e in emits if e[0] == "widget.api_cancel"]
    cancelled_corrs = {e[2]["correlation_id"] for e in cancels}
    assert cancelled_corrs == {"corr_a", "corr_b"}
    for c in cancels:
        assert c[2]["card_id"] == cid
        assert c[2]["reason"] == "card_disposed"
    # The other card's correlation is untouched.
    assert "corr_c" in {e for e in cancelled_corrs} or api_reg.get("corr_c") is not None
    # interrupts called for the right agents.
    assert sorted(interrupted) == ["a", "b"]


def test_inbound_widget_disposed_cancels_inflight_silently(monkeypatch):
    """When the user closes a card client-side, the client emits widget.disposed
    AND it MAY also emit widget.api_cancel separately. Server-side, on receiving
    widget.disposed we silently cancel correlations (no outbound widget.api_cancel
    — the client already knows the card is gone)."""
    sid, key, sess, cid = _setup("sess-cli-disp", "key-cli-disp")
    emits = []
    monkeypatch.setattr(server, "_emit", lambda *a: emits.append(a))

    interrupted = []
    class _FakeAgent:
        def interrupt(self, message=None):
            interrupted.append(True)

    api_reg = sess["api_call_registry"]
    api_reg.register(correlation_id="corr_d", card_id=cid, capability="hermes.ask", agent_ref=_FakeAgent())

    server.dispatch({
        "jsonrpc": "2.0",
        "method": "event",
        "params": {
            "type": "widget.disposed",
            "session_id": sid,
            "payload": {"card_id": cid, "reason": "user_closed"},
        },
    })

    assert api_reg.get("corr_d") is None
    assert interrupted == [True]
    # No outbound widget.api_cancel — the user closed it.
    assert not any(e[0] == "widget.api_cancel" for e in emits)
```

- [ ] **Step 2: Run tests to verify they fail**

```
scripts/run_tests.sh tests/tui_gateway/test_widget_api_cancel_card_disposal.py -v
```

Expected: FAIL.

- [ ] **Step 3: Update `_widget_dispose` to cascade-cancel correlations**

In `tools/widget_tools.py`, modify `_widget_dispose`:

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
    api_reg = sess.get("api_call_registry")

    disposed, already = reg.dispose(card_id, reason=reason)
    if disposed:
        # Before the dispose event, cancel any in-flight calls tied to this card.
        if api_reg is not None:
            cancelled_correlations = api_reg.cancel_for_card(card_id, reason="card_disposed")
            for corr_id in cancelled_correlations:
                _emit_widget_event(
                    "widget.api_cancel",
                    sid,
                    {
                        "correlation_id": corr_id,
                        "card_id": card_id,
                        "reason": "card_disposed",
                    },
                )
        _emit_widget_event(
            "widget.dispose",
            sid,
            {"card_id": card_id, "reason": reason},
        )
    return json.dumps({"disposed": disposed, "already_disposed": already}, ensure_ascii=False)
```

In `tui_gateway/widget_runtime.py`, extend `_on_disposed` (the existing inbound handler from Plan 02):

```python
@event_handler("widget.disposed")
def _on_disposed(params: dict) -> None:
    sid = params.get("session_id", "")
    payload = params.get("payload") or {}
    card_id = payload.get("card_id", "")

    sessions = _state_for_session_safe(sid)
    sess = (sessions or {}).get(sid) if sessions else None
    if not sess:
        return

    # Cancel any in-flight api_call correlations for this card. We do NOT
    # emit outbound widget.api_cancel — the client already knows.
    api_reg = sess.get("api_call_registry")
    if api_reg is not None:
        cancelled = api_reg.cancel_for_card(card_id, reason="user_closed")
        for corr_id in cancelled:
            entry = ApiCallEntry(  # snapshot returned by cancel was already consumed; pull from cancel_for_card return values.
                correlation_id=corr_id, card_id=card_id, capability="", agent_ref=None,
                created_at=0, cancelled_at=None, cancel_reason="user_closed",
            )
            # cancel_for_card returns ids only — to call interrupt we'd need the agent_refs.
            # Refactor cancel_for_card to return entries instead so we keep agent_ref.
```

Refactor `ApiCallRegistry.cancel_for_card` to return the list of cancelled `ApiCallEntry` objects instead of just ids, so the caller can call `interrupt()` on each `agent_ref`:

```python
def cancel_for_card(self, card_id: str, reason: str) -> list[ApiCallEntry]:
    cancelled: list[ApiCallEntry] = []
    with self._lock:
        ids = [c for c, e in self._inflight.items() if e.card_id == card_id]
        for c in ids:
            entry = self._inflight.pop(c, None)
            if entry is not None:
                entry.cancelled_at = time.time()
                entry.cancel_reason = reason
                cancelled.append(entry)
    return cancelled
```

Update `_widget_dispose` and `_on_disposed` to call `interrupt()` on each entry's `agent_ref`:

```python
# in _widget_dispose, after cancelling:
cancelled = api_reg.cancel_for_card(card_id, reason="card_disposed")
for entry in cancelled:
    if entry.agent_ref is not None:
        try:
            entry.agent_ref.interrupt()
        except Exception:
            pass
    _emit_widget_event(
        "widget.api_cancel", sid,
        {"correlation_id": entry.correlation_id, "card_id": card_id, "reason": "card_disposed"},
    )

# in _on_disposed, after cancelling: call interrupt(); do NOT emit outbound api_cancel
cancelled = api_reg.cancel_for_card(card_id, reason="user_closed")
for entry in cancelled:
    if entry.agent_ref is not None:
        try:
            entry.agent_ref.interrupt()
        except Exception:
            pass
```

Update Plan 03's `test_cancel_for_card_returns_all_correlations_for_that_card` test (which expects ids) to check for entries instead — change `assert sorted(cancelled) == ["corr_1", "corr_2"]` to `assert sorted(e.correlation_id for e in cancelled) == ["corr_1", "corr_2"]`.

- [ ] **Step 4: Run tests to verify they pass**

```
scripts/run_tests.sh tests/tui_gateway/test_widget_api_cancel_card_disposal.py tests/tui_gateway/test_api_call_registry.py tests/tools/test_widget_tools_lifecycle.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```
git add tools/widget_tools.py tui_gateway/widget_runtime.py tests/tui_gateway/test_widget_api_cancel_card_disposal.py tests/tui_gateway/test_api_call_registry.py
git commit -m "feat(widget): card disposal cascades cancel + interrupt to all in-flight calls"
```

---

## Task 3: Drop-on-arrival — cancelled btw result is not emitted

**Files:**
- Modify: `tui_gateway/server.py:_spawn_widget_api_call_worker` — check if the correlation was cancelled before emitting `widget.api_response`.
- Test: `tests/tui_gateway/test_widget_api_cancel_drop_on_arrival.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/tui_gateway/test_widget_api_cancel_drop_on_arrival.py
"""When a btw produces a result after cancellation, the result is dropped.

This is the second half of cancellation correctness — interrupt() is
best-effort, and a btw that already finished computing before the cancel
arrived must not have its widget.api_response leak through.
"""

import threading
import time
import types

from tui_gateway import server, widget_runtime


def _setup(sid="sess-drop", key="key-drop"):
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
    sess = state.sessions[sid]
    cid = sess["widget_registry"].allocate(source="x", capabilities=["hermes.ask"], title=None, initial_size=None, trace_id=None)
    return sid, key, sess, cid


def test_btw_result_dropped_after_cancel(monkeypatch):
    sid, key, sess, cid = _setup("sess-drop-1", "key-drop-1")
    emits = []
    monkeypatch.setattr(server, "_emit", lambda *a: emits.append(a))

    proceed = threading.Event()

    class _SlowAgent:
        def __init__(self, *a, **kw): pass
        def interrupt(self, message=None): pass
        def run_conversation(self, text, conversation_history=None):
            # Block until the test gives the OK to "complete".
            proceed.wait(timeout=5.0)
            return {"final_response": "answer"}

    monkeypatch.setattr("run_agent.AIAgent", _SlowAgent)

    server._spawn_widget_api_call_worker(
        sid=sid, session_key=key, correlation_id="corr_late",
        card_id=cid, capability="hermes.ask", call_args={"prompt": "x"},
        history_snapshot=[],
    )
    # Let the worker register its agent_ref.
    time.sleep(0.05)

    # User closes the card; cancellation runs.
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "event",
        "params": {
            "type": "widget.api_cancel",
            "session_id": sid,
            "payload": {"correlation_id": "corr_late", "card_id": cid, "reason": "user_cancelled"},
        },
    })

    # Now release the slow agent — it produces a response, but the
    # correlation is cancelled, so worker must NOT emit widget.api_response.
    proceed.set()
    time.sleep(0.2)

    api_responses = [e for e in emits if e[0] == "widget.api_response"]
    assert api_responses == [], f"cancelled correlation must not emit response; got {api_responses!r}"


def test_post_cancel_runtime_observability(monkeypatch):
    """The ApiCallEntry cancellation snapshot records cancelled_at.
    The worker should log the post-cancel runtime when it eventually
    finishes — but should NOT emit a widget.api_response."""
    sid, key, sess, cid = _setup("sess-drop-obs", "key-drop-obs")
    monkeypatch.setattr(server, "_emit", lambda *a: None)

    api_reg = sess["api_call_registry"]
    entry = api_reg.register(correlation_id="corr_obs", card_id=cid, capability="hermes.ask", agent_ref=None)
    snapshot = api_reg.cancel("corr_obs", reason="card_disposed")

    # Snapshot has cancelled_at recorded.
    assert snapshot.cancelled_at is not None
    # Worker needs a way to compute "how long did btw run after cancel".
    # The snapshot is a complete record; Plan 04's worker logs this on
    # late completion. The test just confirms cancelled_at is on the snapshot.
```

- [ ] **Step 2: Run tests to verify they fail**

```
scripts/run_tests.sh tests/tui_gateway/test_widget_api_cancel_drop_on_arrival.py -v
```

Expected: FAIL — worker still emits `widget.api_response` even after cancellation.

- [ ] **Step 3: Update the worker to check for cancellation before emit**

In `tui_gateway/server.py:_spawn_widget_api_call_worker`, modify the completion block:

```python
def _spawn_widget_api_call_worker(*, sid, session_key, correlation_id, card_id, capability, call_args, history_snapshot):
    state = _state()

    def run():
        session_tokens = []
        try:
            session_tokens = _set_session_context(session_key)
            from run_agent import AIAgent

            sess = state.sessions.get(sid) or {}
            api_reg = sess.get("api_call_registry")

            entry = api_reg.get(correlation_id) if api_reg is not None else None

            if capability == "hermes.ask":
                btw_agent = AIAgent(
                    model=_resolve_model(),
                    quiet_mode=True,
                    platform="tui",
                    max_iterations=8,
                    enabled_toolsets=[],
                )
                if entry is not None:
                    entry.agent_ref = btw_agent

                prompt = str(call_args.get("prompt", "") or "")
                result = btw_agent.run_conversation(prompt, conversation_history=history_snapshot)
                answer = (
                    result.get("final_response", str(result))
                    if isinstance(result, dict) else str(result)
                )
                payload_result = {"answer": answer}
            else:
                _emit_api_response_error(sid, correlation_id, card_id, ERROR_UNKNOWN_CAPABILITY,
                                         f"unsupported capability {capability!r}")
                if api_reg is not None:
                    api_reg.complete(correlation_id)
                return

            # CANCELLATION CHECK: did the correlation get cancelled while we
            # were running? If so, drop the response and log post-cancel runtime.
            still_active = api_reg.get(correlation_id) is not None if api_reg is not None else True
            if not still_active:
                # The correlation was cancelled — it's no longer in the
                # active map, so neither api_reg.complete nor api_reg.cancel
                # will find it. The cancellation snapshot is gone (cancel()
                # popped + returned the snapshot to the caller). Just log
                # and return without emitting.
                logging.getLogger(__name__).info(
                    "[widget] dropping late response for cancelled correlation %s on card %s",
                    correlation_id, card_id,
                )
                return

            # Cap enforcement (unchanged from Plan 03).
            serialized = json.dumps(payload_result, ensure_ascii=False)
            actual = len(serialized.encode("utf-8"))
            if actual > HERMES_ASK_RESPONSE_CAP_BYTES:
                _emit_api_response_error(
                    sid, correlation_id, card_id, ERROR_RESPONSE_TOO_LARGE,
                    f"widget.api_response payload {actual} bytes exceeds cap of "
                    f"{HERMES_ASK_RESPONSE_CAP_BYTES} bytes",
                )
            else:
                _emit("widget.api_response", sid, {
                    "correlation_id": correlation_id,
                    "card_id": card_id,
                    "result": payload_result,
                })

            if api_reg is not None:
                api_reg.complete(correlation_id)
        except Exception as exc:
            # If the call was cancelled, the exception is likely from the
            # interrupt; drop silently.
            if api_reg is not None and api_reg.get(correlation_id) is None:
                logging.getLogger(__name__).debug(
                    "[widget] worker raised after cancel for %s: %s",
                    correlation_id, exc,
                )
                return
            _emit_api_response_error(sid, correlation_id, card_id, 5103,
                                     f"widget.api_call worker error: {exc}")
            if api_reg is not None:
                api_reg.complete(correlation_id)
        finally:
            if session_tokens:
                _clear_session_context(session_tokens)

    threading.Thread(target=run, daemon=True).start()
```

- [ ] **Step 4: Run tests to verify they pass**

```
scripts/run_tests.sh tests/tui_gateway/test_widget_api_cancel_drop_on_arrival.py tests/tui_gateway/test_widget_api_call_handler.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```
git add tui_gateway/server.py tests/tui_gateway/test_widget_api_cancel_drop_on_arrival.py
git commit -m "feat(tui_gateway): drop late btw response when correlation has been cancelled"
```

---

## Task 4: Cross-machine alignment — `widget.api_cancel` envelope

**Files:**
- Test: `tests/tui_gateway/test_widget_api_cancel_envelope_alignment.py`

- [ ] **Step 1: Add the test**

```python
# tests/tui_gateway/test_widget_api_cancel_envelope_alignment.py
"""Cross-machine: widget.api_cancel envelope shape (client → server and server → client).

Spec §3.5.4 — payload carries exactly {correlation_id, card_id, reason}.
The Tauri side sends + receives this exact shape; deviating breaks the wire.
"""

import threading
import types

from tui_gateway import server, widget_runtime


def _setup(sid="sess-env-cx", key="key-env-cx"):
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
    return sid, key, state.sessions[sid]


def test_outbound_widget_api_cancel_payload_shape(monkeypatch):
    """Outbound emit on card disposal carries exactly the three fields."""
    from tools.registry import registry
    import json

    sid, key, sess = _setup("sess-env-out", "key-env-out")
    cid = sess["widget_registry"].allocate(source="x", capabilities=["hermes.ask"], title=None, initial_size=None, trace_id=None)
    sess["api_call_registry"].register(correlation_id="corr_e", card_id=cid, capability="hermes.ask", agent_ref=None)

    emits = []
    monkeypatch.setattr(server, "_emit", lambda *a: emits.append(a))

    handler = registry.get_entry("widget_dispose").handler
    json.loads(handler({"card_id": cid, "reason": "task_complete"}, session_id=key))

    api_cancels = [e for e in emits if e[0] == "widget.api_cancel"]
    assert len(api_cancels) == 1
    payload = api_cancels[0][2]
    assert set(payload.keys()) == {"correlation_id", "card_id", "reason"}
    assert payload["correlation_id"] == "corr_e"
    assert payload["card_id"] == cid
    assert payload["reason"] == "card_disposed"


def test_inbound_widget_api_cancel_envelope_routes_correctly():
    """Inbound: jsonrpc=2.0, method=event, no id, params.type=widget.api_cancel."""
    sid, key, sess = _setup("sess-env-in", "key-env-in")
    cid = sess["widget_registry"].allocate(source="x", capabilities=["hermes.ask"], title=None, initial_size=None, trace_id=None)
    sess["api_call_registry"].register(correlation_id="corr_in", card_id=cid, capability="hermes.ask", agent_ref=None)

    resp = server.dispatch({
        "jsonrpc": "2.0",
        "method": "event",
        # NO id field — matches Tauri-side outbound envelope.
        "params": {
            "type": "widget.api_cancel",
            "session_id": sid,
            "payload": {"correlation_id": "corr_in", "card_id": cid, "reason": "user_cancelled"},
        },
    })
    assert resp is None  # events have no response
    # Correlation is gone from the registry.
    assert sess["api_call_registry"].get("corr_in") is None


def test_all_four_widget_inbound_events_route():
    """All four widget.* event types must be registered: mounted, error, disposed, api_cancel."""
    expected = {"widget.mounted", "widget.error", "widget.disposed", "widget.api_cancel"}
    assert expected.issubset(set(server._event_handlers))
```

- [ ] **Step 2: Run the test**

```
scripts/run_tests.sh tests/tui_gateway/test_widget_api_cancel_envelope_alignment.py -v
```

Expected: 3 passed.

- [ ] **Step 3: Commit**

```
git add tests/tui_gateway/test_widget_api_cancel_envelope_alignment.py
git commit -m "test(tui_gateway): widget.api_cancel envelope shape alignment"
```

---

## Task 5: Session-end cancellation — outbound `widget.api_cancel` for every in-flight correlation

**Files:**
- Modify: `tui_gateway/server.py:session.close` (or `_unregister_session`) — emit `widget.api_cancel` with `reason: "session_ended"` for every correlation in the per-session `ApiCallRegistry` before tearing down.
- Test: extend `tests/tui_gateway/test_widget_api_cancel_card_disposal.py` (or a new file).

- [ ] **Step 1: Add failing test**

```python
# tests/tui_gateway/test_widget_api_cancel_session_end.py
"""Session close emits widget.api_cancel(reason='session_ended') for each in-flight call."""

import threading
import types

from tui_gateway import server, widget_runtime


def test_session_close_emits_api_cancel_per_inflight(monkeypatch):
    sid, key = "sess-end", "key-end"
    transport = types.SimpleNamespace(write=lambda *a, **k: True)
    state = server._state()
    state.sessions[sid] = {
        "session_key": key,
        "transport": transport,
        "client_capabilities": ["widget.render"],
        "widget_registry": widget_runtime.WidgetRegistry(),
        "api_call_registry": widget_runtime.ApiCallRegistry(),
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "slash_worker": None,
    }
    server._register_session(sid)
    cid = state.sessions[sid]["widget_registry"].allocate(source="x", capabilities=["hermes.ask"], title=None, initial_size=None, trace_id=None)
    api_reg = state.sessions[sid]["api_call_registry"]
    api_reg.register(correlation_id="corr_a", card_id=cid, capability="hermes.ask", agent_ref=None)
    api_reg.register(correlation_id="corr_b", card_id=cid, capability="hermes.ask", agent_ref=None)

    emits = []
    monkeypatch.setattr(server, "_emit", lambda *a: emits.append(a))

    handler = server._methods["session.close"]
    handler(1, {"session_id": sid})

    cancels = [e for e in emits if e[0] == "widget.api_cancel"]
    assert {e[2]["correlation_id"] for e in cancels} == {"corr_a", "corr_b"}
    for c in cancels:
        assert c[2]["reason"] == "session_ended"
```

- [ ] **Step 2: Run the test**

```
scripts/run_tests.sh tests/tui_gateway/test_widget_api_cancel_session_end.py -v
```

Expected: FAIL — session.close doesn't iterate the api_call_registry.

- [ ] **Step 3: Wire session-end cancellation**

Locate the `session.close` handler in `tui_gateway/server.py` (search `@method("session.close")`). Before popping the session dict, iterate `api_call_registry` and emit `widget.api_cancel` for each:

```python
@method("session.close")
def _(rid, params: dict) -> dict:
    sid = params.get("session_id", "") or ""
    state = _state()
    sess = state.sessions.get(sid)
    if sess is not None:
        api_reg = sess.get("api_call_registry")
        if api_reg is not None:
            # cancel_for_card iterates by card; here we cancel everything in-flight.
            with api_reg._lock:
                inflight_ids = list(api_reg._inflight.keys())
            for corr_id in inflight_ids:
                entry = api_reg.cancel(corr_id, reason="session_ended")
                if entry is not None:
                    _emit("widget.api_cancel", sid, {
                        "correlation_id": corr_id,
                        "card_id": entry.card_id,
                        "reason": "session_ended",
                    })
                    if entry.agent_ref is not None:
                        try:
                            entry.agent_ref.interrupt()
                        except Exception:
                            pass
        # ... existing cleanup (slash_worker close, _unregister_session, pop, etc.)
```

If reaching directly into `api_reg._inflight` feels too coupled, add a small helper `ApiCallRegistry.snapshot_inflight() -> list[ApiCallEntry]` that returns a thread-safe shallow copy.

- [ ] **Step 4: Run the test**

```
scripts/run_tests.sh tests/tui_gateway/test_widget_api_cancel_session_end.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```
git add tui_gateway/server.py tests/tui_gateway/test_widget_api_cancel_session_end.py
git commit -m "feat(tui_gateway): session.close cancels in-flight api_calls with session_ended reason"
```

---

## Task 6: End-to-end smoke test — render → ask → cancel → no response

**Files:**
- Test: `tests/tui_gateway/test_widget_cancel_e2e.py`

- [ ] **Step 1: Add the test**

```python
# tests/tui_gateway/test_widget_cancel_e2e.py
"""End-to-end: render, kick off hermes.ask, dispose mid-flight, no widget.api_response."""

import json
import threading
import time
import types

from tools.registry import registry
from tui_gateway import server, widget_runtime


def test_render_then_dispose_mid_ask(monkeypatch):
    sid, key = "sess-e2e", "key-e2e"
    transport = types.SimpleNamespace(write=lambda *a, **k: True)
    state = server._state()
    state.sessions[sid] = {
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
    server._register_session(sid)

    emits = []
    monkeypatch.setattr(server, "_emit", lambda *a: emits.append(a))

    # Render: mock the mount.
    reg = state.sessions[sid]["widget_registry"]

    def fake_mount():
        for _ in range(100):
            with reg._lock:
                cards = list(reg._cards.keys())
            if cards:
                reg.mark_mounted(cards[0], compiled_size=1024, compile_ms=8)
                return
            threading.Event().wait(0.01)

    threading.Thread(target=fake_mount, daemon=True).start()
    render_handler = registry.get_entry("render_widget").handler
    render_result = json.loads(render_handler(
        {"source": "export default function C(){return null}", "capabilities": ["hermes.ask"]},
        session_id=key,
    ))
    cid = render_result["card_id"]

    # Kick off a slow widget.api_call.
    proceed = threading.Event()
    class _SlowAgent:
        def __init__(self, *a, **kw): pass
        def interrupt(self, message=None): pass
        def run_conversation(self, text, conversation_history=None):
            proceed.wait(timeout=5.0)
            return {"final_response": "answer that arrives too late"}
    monkeypatch.setattr("run_agent.AIAgent", _SlowAgent)

    api_call_handler = server._methods["widget.api_call"]
    ack = api_call_handler(99, {
        "session_id": sid, "card_id": cid, "correlation_id": "corr_e2e",
        "capability": "hermes.ask", "args": {"prompt": "x"},
    })
    assert ack["result"]["accepted"] is True
    time.sleep(0.05)  # let worker start

    # Dispose the card mid-flight.
    dispose_handler = registry.get_entry("widget_dispose").handler
    json.loads(dispose_handler({"card_id": cid, "reason": "task_complete"}, session_id=key))

    # widget.api_cancel emitted for the correlation.
    assert any(e[0] == "widget.api_cancel" and e[2]["correlation_id"] == "corr_e2e" for e in emits)

    # Now release the slow agent — its response must be dropped.
    proceed.set()
    time.sleep(0.2)
    assert not any(e[0] == "widget.api_response" for e in emits), \
        "cancelled correlation must not emit a widget.api_response"
```

- [ ] **Step 2: Run the test**

```
scripts/run_tests.sh tests/tui_gateway/test_widget_cancel_e2e.py -v
```

Expected: PASS — all the pieces from Plans 02–04 cooperate.

- [ ] **Step 3: Commit**

```
git add tests/tui_gateway/test_widget_cancel_e2e.py
git commit -m "test(tui_gateway): render→ask→dispose-mid-flight produces no zombie response"
```

---

## Acceptance for Plan 04

- Inbound `widget.api_cancel` event handler is registered, looks up the correlation in the per-session `ApiCallRegistry`, calls `agent.interrupt()` on the stashed `agent_ref` (best-effort), records `cancel_reason` and `cancelled_at` for observability, and pops the entry.
- `widget_dispose` tool cascades cancellation: every correlation tied to the disposed card is cancelled, each gets an outbound `widget.api_cancel` event with `reason: "card_disposed"`, and each agent's `interrupt()` is called.
- Inbound `widget.disposed` event also cascades cancellation but does NOT emit outbound `widget.api_cancel` (the client already knows the card is gone).
- The worker that runs `prompt.btw` checks for cancellation before emitting `widget.api_response`. A late-arriving result is dropped silently with a debug log; no `widget.api_response` is emitted for cancelled correlations.
- `session.close` iterates the `ApiCallRegistry` and emits `widget.api_cancel` with `reason: "session_ended"` for each in-flight correlation, calls `agent.interrupt()`, then tears down the session dict.
- Cross-machine alignment: outbound and inbound `widget.api_cancel` payloads carry exactly `{correlation_id, card_id, reason}`. The full event-handlers set is `{widget.mounted, widget.error, widget.disposed, widget.api_cancel}`.
- Lite observability: `ApiCallEntry.cancelled_at` and `cancel_reason` are set on cancellation; the snapshot lets a future plan compute "post-cancel runtime" if late completions become a concern.

Plans 01–04 together close the cancellation path. Plan 05 fills the example-discovery tools so the agent is actually good at writing widgets.
