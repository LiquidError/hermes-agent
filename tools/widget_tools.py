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

registry.register(
    name="render_widget",
    toolset="widget",
    schema=RENDER_WIDGET_SCHEMA,
    handler=lambda args, **kw: _stub("render_widget"),
    check_fn=_check,
    emoji="🪟",
)

registry.register(
    name="widget_update",
    toolset="widget",
    schema=WIDGET_UPDATE_SCHEMA,
    handler=lambda args, **kw: _stub("widget_update"),
    check_fn=_check,
    emoji="🪟",
)

registry.register(
    name="widget_message",
    toolset="widget",
    schema=WIDGET_MESSAGE_SCHEMA,
    handler=lambda args, **kw: _stub("widget_message"),
    check_fn=_check,
    emoji="🪟",
)

registry.register(
    name="widget_dispose",
    toolset="widget",
    schema=WIDGET_DISPOSE_SCHEMA,
    handler=lambda args, **kw: _stub("widget_dispose"),
    check_fn=_check,
    emoji="🪟",
)

registry.register(
    name="list_widget_examples",
    toolset="widget",
    schema=LIST_WIDGET_EXAMPLES_SCHEMA,
    handler=lambda args, **kw: _stub("list_widget_examples"),
    check_fn=_check,
    emoji="🪟",
)

registry.register(
    name="read_widget_example",
    toolset="widget",
    schema=READ_WIDGET_EXAMPLE_SCHEMA,
    handler=lambda args, **kw: _stub("read_widget_example"),
    check_fn=_check,
    emoji="🪟",
)
