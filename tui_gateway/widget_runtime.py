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
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Optional

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
        reg.dispose(
            payload.get("card_id", ""),
            reason=str(payload.get("reason", "user_closed")),
        )
