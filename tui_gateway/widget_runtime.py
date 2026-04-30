"""Widget runtime — capability-gate primitives and per-session registry.

Hosts:
  - the per-context flag the widget-tool ``check_fn`` hooks read at agent
    construction time.
  - ``WidgetRegistry``: per-session map of live widget cards keyed by
    ``card_id``. Owns card-id allocation, mount-resolution Event, and
    idempotent disposal.
"""

from __future__ import annotations

import secrets
import threading
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, Optional

_WIDGET_RENDER_AVAILABLE: ContextVar[bool] = ContextVar(
    "widget_render_available", default=False
)


def set_widget_render_available(value: bool) -> Token:
    return _WIDGET_RENDER_AVAILABLE.set(bool(value))


def reset_widget_render_available(token: Token) -> None:
    _WIDGET_RENDER_AVAILABLE.reset(token)


def is_widget_render_available() -> bool:
    return _WIDGET_RENDER_AVAILABLE.get()


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
                entry._resolution = (
                    "error",
                    {
                        "phase": "dispose",
                        "kind": "disposed_before_mount",
                        "message": "card disposed before mount resolved",
                        "stack": "",
                    },
                )
                entry._resolved.set()
            return (True, False)


@dataclass
class ApiCallEntry:
    correlation_id: str
    card_id: str
    capability: str
    # The AIAgent running the prompt.btw — used by Plan 04 for cancellation.
    agent_ref: Any
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

    def snapshot_inflight(self) -> list[ApiCallEntry]:
        """Return a thread-safe shallow copy of all in-flight entries."""
        with self._lock:
            return list(self._inflight.values())

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


def _registry_for(session_id: str) -> Optional["WidgetRegistry"]:
    """Look up the per-session WidgetRegistry by sid. Returns None if no session."""
    from tui_gateway.server import _state_for_session

    state = _state_for_session(session_id)
    sess = state.sessions.get(session_id) or {}
    return sess.get("widget_registry")


def _state_for_session_safe(sid: str):
    """Return the sessions dict for a session id, or None if not registered."""
    from tui_gateway.server import _state_for_session

    if not sid:
        return None
    state = _state_for_session(sid)
    return state.sessions if state is not None else None


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
        card_id = payload.get("card_id", "")
        reg = _registry_for(sid)
        if reg is None:
            return
        reg.dispose(
            card_id,
            reason=str(payload.get("reason", "user_closed")),
        )

        # Cascade-cancel any in-flight api_call correlations for this card.
        # Do NOT emit outbound widget.api_cancel — the client already knows
        # the card is gone.
        sessions = _state_for_session_safe(sid)
        sess = (sessions or {}).get(sid) if sessions else None
        if not sess:
            return
        api_reg = sess.get("api_call_registry")
        if api_reg is None:
            return
        cancelled = api_reg.cancel_for_card(card_id, reason="user_closed")
        for entry in cancelled:
            if entry.agent_ref is not None:
                try:
                    entry.agent_ref.interrupt()
                except Exception:
                    pass

    @event_handler("widget.api_cancel")
    def _on_api_cancel(params: dict) -> None:
        sid = params.get("session_id", "")
        payload = params.get("payload") or {}
        correlation_id = str(payload.get("correlation_id", "") or "")
        reason = str(payload.get("reason", "user_cancelled") or "user_cancelled")

        sessions = _state_for_session_safe(sid)
        sess = (sessions or {}).get(sid) if sessions else None
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
                # Best-effort. Worker continues; drop-on-arrival catches it.
                pass
