"""End-to-end: list_widget_examples → read_widget_example over the four starters.

This is the workflow the addendum prescribes — list to discover, read to
learn the pattern, then render. Each example must include a Capabilities
line so the agent knows what to declare in render_widget.
"""

import json

from tools import widget_tools  # noqa: F401  triggers registration
from tools.registry import registry


def test_list_then_read_workflow():
    list_handler = registry.get_entry("list_widget_examples").handler
    listed = json.loads(list_handler({}))
    assert listed["examples"], "no starter examples shipped"

    read_handler = registry.get_entry("read_widget_example").handler
    for item in listed["examples"]:
        result = json.loads(read_handler({"name": item["name"]}))
        assert "content" in result, f"failed to read {item['name']}: {result!r}"
        assert "Capabilities" in result["content"]
