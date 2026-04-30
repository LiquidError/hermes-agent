"""WidgetRegistry: card-id allocation, mount resolution, idempotent disposal."""

import re
import threading
import time

import pytest

from tui_gateway.widget_runtime import WidgetRegistry, CardEntry


CARD_ID_RE = re.compile(r"^wgt_[0-9a-f]{6}$")


def test_allocate_returns_well_formed_card_id():
    reg = WidgetRegistry()
    card_id = reg.allocate(source="x", capabilities=[], title=None, initial_size=None, trace_id=None)
    assert CARD_ID_RE.match(card_id)


def test_allocate_returns_unique_ids():
    reg = WidgetRegistry()
    seen = {reg.allocate(source="x", capabilities=[], title=None, initial_size=None, trace_id=None) for _ in range(50)}
    assert len(seen) == 50


def test_get_returns_entry_for_live_card():
    reg = WidgetRegistry()
    cid = reg.allocate(source="src", capabilities=["hermes.ask"], title="t", initial_size={"w": 400, "h": 300}, trace_id="tc_1")
    entry = reg.get(cid)
    assert isinstance(entry, CardEntry)
    assert entry.card_id == cid
    assert entry.capabilities == ["hermes.ask"]
    assert entry.title == "t"
    assert entry.initial_size == {"w": 400, "h": 300}
    assert entry.trace_id == "tc_1"


def test_get_returns_none_for_unknown_card():
    reg = WidgetRegistry()
    assert reg.get("wgt_000000") is None


def test_wait_for_mount_resolves_when_marked_mounted():
    reg = WidgetRegistry()
    cid = reg.allocate(source="x", capabilities=[], title=None, initial_size=None, trace_id=None)

    def mount_later():
        time.sleep(0.05)
        reg.mark_mounted(cid, compiled_size=4823, compile_ms=12)

    threading.Thread(target=mount_later, daemon=True).start()
    result = reg.wait_for_mount(cid, timeout=2.0)
    assert result == ("mounted", {"compiled_size": 4823, "compile_ms": 12})


def test_wait_for_mount_resolves_with_error():
    reg = WidgetRegistry()
    cid = reg.allocate(source="x", capabilities=[], title=None, initial_size=None, trace_id=None)

    def err_later():
        time.sleep(0.05)
        reg.mark_error(cid, phase="compile", kind="syntax_error", message="oops", stack="trace")

    threading.Thread(target=err_later, daemon=True).start()
    status, payload = reg.wait_for_mount(cid, timeout=2.0)
    assert status == "error"
    assert payload["phase"] == "compile"
    assert payload["kind"] == "syntax_error"
    assert payload["message"] == "oops"


def test_wait_for_mount_times_out():
    reg = WidgetRegistry()
    cid = reg.allocate(source="x", capabilities=[], title=None, initial_size=None, trace_id=None)
    result = reg.wait_for_mount(cid, timeout=0.05)
    assert result == ("timeout", None)


def test_dispose_returns_true_for_live_card():
    reg = WidgetRegistry()
    cid = reg.allocate(source="x", capabilities=[], title=None, initial_size=None, trace_id=None)
    disposed, already = reg.dispose(cid, reason="task_complete")
    assert disposed is True
    assert already is False
    assert reg.get(cid) is None


def test_dispose_is_idempotent_on_unknown_card():
    reg = WidgetRegistry()
    disposed, already = reg.dispose("wgt_000000", reason="task_complete")
    assert disposed is False
    assert already is True


def test_dispose_is_idempotent_on_already_disposed_card():
    reg = WidgetRegistry()
    cid = reg.allocate(source="x", capabilities=[], title=None, initial_size=None, trace_id=None)
    reg.dispose(cid, reason="task_complete")
    disposed, already = reg.dispose(cid, reason="task_complete")
    assert disposed is False
    assert already is True


def test_update_source_on_live_card():
    reg = WidgetRegistry()
    cid = reg.allocate(source="old", capabilities=["hermes.ask"], title="t", initial_size=None, trace_id=None)
    updated, gone = reg.update_source(cid, source="new", capabilities=["notes.save"])
    assert updated is True
    assert gone is False
    entry = reg.get(cid)
    assert entry.source == "new"
    assert entry.capabilities == ["notes.save"]


def test_update_source_preserves_capabilities_when_omitted():
    reg = WidgetRegistry()
    cid = reg.allocate(source="old", capabilities=["hermes.ask"], title="t", initial_size=None, trace_id=None)
    reg.update_source(cid, source="new", capabilities=None)
    assert reg.get(cid).capabilities == ["hermes.ask"]


def test_update_source_on_disposed_card_signals_gone():
    reg = WidgetRegistry()
    updated, gone = reg.update_source("wgt_000000", source="x", capabilities=None)
    assert updated is False
    assert gone is True
