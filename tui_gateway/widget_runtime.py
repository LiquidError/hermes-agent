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
