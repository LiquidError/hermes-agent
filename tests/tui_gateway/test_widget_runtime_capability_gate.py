import threading

from tui_gateway import widget_runtime


def test_default_is_false():
    assert widget_runtime.is_widget_render_available() is False


def test_set_then_read_returns_true():
    token = widget_runtime.set_widget_render_available(True)
    try:
        assert widget_runtime.is_widget_render_available() is True
    finally:
        widget_runtime.reset_widget_render_available(token)
    assert widget_runtime.is_widget_render_available() is False


def test_per_thread_isolation_via_contextvar():
    seen = {}

    def worker():
        seen["thread"] = widget_runtime.is_widget_render_available()

    token = widget_runtime.set_widget_render_available(True)
    try:
        t = threading.Thread(target=worker)
        t.start()
        t.join()
    finally:
        widget_runtime.reset_widget_render_available(token)

    assert seen["thread"] is False
    # contextvars don't propagate to a bare threading.Thread; this
    # confirms the gate is per-context, which matches how check_fn is
    # called during agent construction.
