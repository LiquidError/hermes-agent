"""Worker-thread access to per-connection session and prompt state.

Background threads (agent loop, widget tools, slash worker callbacks) do not
inherit the request's ``tui_gateway_state`` contextvar binding, so a naive
``_state()`` lookup falls back to ``_default_state`` and misses sessions or
pending prompts that live on a per-connection state.

These tests pin two contracts:

1. ``_resolve_session_by_key`` finds a session by ``session_key`` even when
   the session lives on a non-default state and the caller has no contextvar
   binding (the path widget tools take from agent threads).
2. ``_block`` writes pending state onto the session-owning state, so a
   ``*.respond`` request that runs on the originating connection's request
   thread can release the prompt.
"""

from __future__ import annotations

import threading

from tui_gateway import server


def _make_session(state, sid: str, session_key: str) -> None:
    state.sessions[sid] = {"session_key": session_key}
    server._register_session(sid)


def test_resolve_session_by_key_finds_session_on_non_default_state():
    bound = server._DispatcherState()
    sid, key = "sid-cross", "key-cross"
    token = server._state_var.set(bound)
    try:
        _make_session(bound, sid, key)
    finally:
        server._state_var.reset(token)

    try:
        # Caller (e.g. widget tool worker) has no contextvar binding.
        assert server._state() is server._default_state
        result = server._resolve_session_by_key(key)
        assert result == (sid, bound.sessions[sid])
    finally:
        bound.sessions.pop(sid, None)
        server._unregister_session(sid)


def test_resolve_session_by_key_returns_none_for_unknown_key():
    assert server._resolve_session_by_key("no-such-key") is None
    assert server._resolve_session_by_key("") is None


def test_block_pending_lives_on_session_owning_state():
    """The prompt event fires from a worker thread; the response arrives on
    the request thread bound to the per-connection state. Pending must live
    on the per-connection state so both sides see the same dict.
    """
    bound = server._DispatcherState()
    sid = "sid-block"
    server._session_states[sid] = bound  # bypass _register_session, no contextvar needed

    captured: dict = {}

    def fake_emit(event, target_sid, payload):
        captured["event"] = event
        captured["sid"] = target_sid
        captured["payload"] = dict(payload)

    orig_emit = server._emit
    server._emit = fake_emit
    try:
        result_holder: dict = {}

        def worker():
            # Worker thread: no contextvar binding, _state() is _default_state.
            assert server._state() is server._default_state
            result_holder["answer"] = server._block(
                "clarify.request", sid, {"question": "what?"}, timeout=2,
            )

        t = threading.Thread(target=worker, daemon=True)
        t.start()

        # Wait for _block to register pending on the per-connection state.
        rid = None
        for _ in range(200):
            if bound.pending:
                rid = next(iter(bound.pending))
                break
            threading.Event().wait(0.01)
        assert rid is not None, "pending entry never landed on per-connection state"
        assert rid not in server._default_state.pending, (
            "pending leaked onto _default_state — _respond would never find it"
        )
        assert captured["event"] == "clarify.request"
        assert captured["payload"]["request_id"] == rid

        # Simulate `clarify.respond` on the originating connection's request
        # thread: contextvar bound to the same per-connection state.
        token = server._state_var.set(bound)
        try:
            entry = server._state().pending.get(rid)
            assert entry is not None, "_respond cannot find rid on per-connection state"
            _, ev = entry
            server._state().answers[rid] = "the answer"
            ev.set()
        finally:
            server._state_var.reset(token)

        t.join(timeout=2)
        assert not t.is_alive(), "_block did not return after the response was set"
        assert result_holder["answer"] == "the answer"
    finally:
        server._emit = orig_emit
        server._session_states.pop(sid, None)


def test_clear_pending_routes_by_session_state():
    """``session.interrupt`` releases prompts on its own session only. The
    pending entry was written by a worker thread via ``_block`` onto the
    per-connection state — ``_clear_pending(sid)`` must look there too.
    """
    bound = server._DispatcherState()
    sid = "sid-clear"
    server._session_states[sid] = bound
    rid = "deadbeef"
    ev = threading.Event()
    bound.pending[rid] = (sid, ev)
    try:
        server._clear_pending(sid)
        assert ev.is_set(), "_clear_pending did not release pending on owning state"
        assert bound.answers.get(rid) == ""
    finally:
        bound.pending.pop(rid, None)
        bound.answers.pop(rid, None)
        server._session_states.pop(sid, None)
