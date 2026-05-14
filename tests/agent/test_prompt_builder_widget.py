"""WIDGET_AUTHOR_GUIDANCE loads from disk and is conditionally included."""

from agent import prompt_builder


def test_guidance_constant_is_non_empty():
    assert isinstance(prompt_builder.WIDGET_AUTHOR_GUIDANCE, str)
    text = prompt_builder.WIDGET_AUTHOR_GUIDANCE
    assert "render_widget" in text
    assert "widget_message" in text
    assert "widget_dispose" in text


def test_guidance_is_short_enough():
    # Lean addendum: ~30 lines target; allow up to 60 lines for slack.
    assert prompt_builder.WIDGET_AUTHOR_GUIDANCE.count("\n") < 60


def test_guidance_does_not_inline_primitives_types():
    # The addendum tells the agent to fetch examples on demand; it should
    # NOT inline the full canvasAPI surface (that's the point of Gap 5
    # in the source spec).
    text = prompt_builder.WIDGET_AUTHOR_GUIDANCE
    assert "interface CanvasAPI" not in text
    assert "type CanvasPrimitive" not in text
