"""Widget tools must auto-register when model_tools is imported.

The AST-based discovery scanner in tools/registry.py walks each tool
module's top-level body looking for `registry.register(...)` calls.
A previous version of widget_tools.py wrapped registration inside a
`for` loop, which the scanner skipped — so the tools never registered
in production. This test guards the regression.
"""

from __future__ import annotations


def test_widget_tools_register_via_model_tools_discovery():
    # Deliberately do NOT import tools.widget_tools here. Discovery alone
    # must populate the registry.
    import importlib
    import model_tools  # noqa: F401  triggers discover_builtin_tools

    importlib.reload(model_tools)

    from tools.registry import registry

    for name in (
        "render_widget", "widget_update", "widget_message", "widget_dispose",
        "list_widget_examples", "read_widget_example",
    ):
        entry = registry.get_entry(name)
        assert entry is not None, f"{name} not auto-discovered"
        assert entry.toolset == "widget"
