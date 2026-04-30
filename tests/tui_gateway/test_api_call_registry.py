"""ApiCallRegistry: register/complete/cancel; observability timestamps."""

import time

from tui_gateway.widget_runtime import ApiCallRegistry, ApiCallEntry


def test_register_returns_entry():
    reg = ApiCallRegistry()
    entry = reg.register(
        correlation_id="corr_a1b2c3",
        card_id="wgt_8a3f9c",
        capability="hermes.ask",
        agent_ref=None,
    )
    assert isinstance(entry, ApiCallEntry)
    assert entry.correlation_id == "corr_a1b2c3"
    assert entry.created_at > 0
    assert entry.cancelled_at is None
    assert entry.completed_at is None


def test_get_returns_entry_for_known_correlation():
    reg = ApiCallRegistry()
    reg.register(correlation_id="corr_x", card_id="wgt_x", capability="hermes.ask", agent_ref=None)
    e = reg.get("corr_x")
    assert e is not None and e.card_id == "wgt_x"


def test_get_returns_none_for_unknown_correlation():
    reg = ApiCallRegistry()
    assert reg.get("corr_missing") is None


def test_complete_marks_completed_and_pops():
    reg = ApiCallRegistry()
    reg.register(correlation_id="corr_y", card_id="wgt_y", capability="hermes.ask", agent_ref=None)
    e = reg.complete("corr_y")
    assert e is not None
    assert e.completed_at is not None
    assert reg.get("corr_y") is None  # popped


def test_complete_returns_none_for_unknown():
    reg = ApiCallRegistry()
    assert reg.complete("corr_nope") is None


def test_cancel_marks_cancelled_and_keeps_entry_for_observability():
    reg = ApiCallRegistry()
    reg.register(correlation_id="corr_z", card_id="wgt_z", capability="hermes.ask", agent_ref=None)
    e = reg.cancel("corr_z", reason="card_disposed")
    assert e is not None
    assert e.cancelled_at is not None
    assert e.cancel_reason == "card_disposed"
    # Cancellation removes the entry from the active map; the returned
    # entry is the snapshot. (Plan 04 wires interrupt + drop-on-arrival.)
    assert reg.get("corr_z") is None


def test_cancel_for_card_returns_all_correlations_for_that_card():
    reg = ApiCallRegistry()
    reg.register(correlation_id="corr_1", card_id="wgt_a", capability="hermes.ask", agent_ref=None)
    reg.register(correlation_id="corr_2", card_id="wgt_a", capability="hermes.ask", agent_ref=None)
    reg.register(correlation_id="corr_3", card_id="wgt_b", capability="hermes.ask", agent_ref=None)

    cancelled = reg.cancel_for_card("wgt_a", reason="card_disposed")
    assert sorted(cancelled) == ["corr_1", "corr_2"]
    assert reg.get("corr_1") is None
    assert reg.get("corr_2") is None
    assert reg.get("corr_3") is not None


def test_post_cancel_runtime_measurement():
    reg = ApiCallRegistry()
    reg.register(correlation_id="corr_o", card_id="wgt_o", capability="hermes.ask", agent_ref=None)
    e_cancelled = reg.cancel("corr_o", reason="card_disposed")
    time.sleep(0.05)
    # Plan 04 will use this for observability when btw still produces a
    # response after cancellation. This test asserts the snapshot has
    # the timestamp we'll diff against.
    assert e_cancelled.cancelled_at is not None
