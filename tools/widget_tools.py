# tools/widget_tools.py
"""Widget tools — agent-facing surface for the canvas widget runtime.

Six tools:
  render_widget          - create a card on the canvas
  widget_update          - replace the source of a live card
  widget_message         - push structured data into a live card
  widget_dispose         - close a live card
  list_widget_examples   - discover example .tsx files
  read_widget_example    - read a specific example file

The capability gate (``check_fn``) and OpenAI schemas are visible to the
agent the moment the client advertises ``widget.render``. The four
lifecycle tools below dispatch through the per-session ``WidgetRegistry``
and emit ``widget.*`` events on the bound transport. The two example
helpers remain stubs until later plans wire them up.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from tools.registry import registry
from tui_gateway.widget_runtime import is_widget_render_available, WidgetRegistry


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
# Helpers
# --------------------------------------------------------------------------


def _stub(tool_name: str) -> str:
    return json.dumps(
        {"error": "not_implemented", "tool": tool_name},
        ensure_ascii=False,
    )


def _check() -> bool:
    return is_widget_render_available()


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
        return json.dumps(
            {
                "card_id": card_id,
                "compiled_size": info.get("compiled_size", 0),
                "compile_ms": info.get("compile_ms", 0),
            },
            ensure_ascii=False,
        )
    if status == "error":
        return _err(
            5101,
            info.get("message", "client refused to mount"),
            phase=info.get("phase", "compile"),
            kind=info.get("kind", "unknown"),
            card_id=card_id,
        )
    # timeout
    return _err(
        5102,
        f"render_widget timed out after {RENDER_TIMEOUT_S}s waiting for widget.mounted",
        card_id=card_id,
    )


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------


_REGISTRATIONS: list[tuple[str, dict]] = [
    ("render_widget", RENDER_WIDGET_SCHEMA),
    ("widget_update", WIDGET_UPDATE_SCHEMA),
    ("widget_message", WIDGET_MESSAGE_SCHEMA),
    ("widget_dispose", WIDGET_DISPOSE_SCHEMA),
    ("list_widget_examples", LIST_WIDGET_EXAMPLES_SCHEMA),
    ("read_widget_example", READ_WIDGET_EXAMPLE_SCHEMA),
]


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
