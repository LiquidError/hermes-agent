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
