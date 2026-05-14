"""Edit guard: the widget addendum mentions every widget tool the agent uses.

If a future tweak removes a tool name from the addendum, the agent will
silently stop calling it and start writing widgets blind to the
primitives surface.
"""

from agent import prompt_builder


def test_addendum_mentions_list_tool():
    assert "list_widget_examples" in prompt_builder.WIDGET_AUTHOR_GUIDANCE


def test_addendum_mentions_read_tool():
    assert "read_widget_example" in prompt_builder.WIDGET_AUTHOR_GUIDANCE


def test_addendum_mentions_lifecycle_tools():
    text = prompt_builder.WIDGET_AUTHOR_GUIDANCE
    for tool in ("render_widget", "widget_update", "widget_message", "widget_dispose"):
        assert tool in text, f"addendum must mention {tool}"
