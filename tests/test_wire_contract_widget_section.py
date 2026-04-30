"""The base wire contract carries a Widget render section that mentions every
event-type and method the implementation plans ship. Edit guard: a wire-shape
change on one side must be reflected here, or the contract drifts silently."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
CONTRACT = REPO_ROOT / "plans" / "tauri-client-contract.md"


def test_contract_has_widget_section():
    text = CONTRACT.read_text(encoding="utf-8")
    assert "Widget render" in text or "widget runtime" in text.lower()


def test_contract_lists_every_widget_event_and_method():
    text = CONTRACT.read_text(encoding="utf-8")
    for shape in (
        "widget.render", "widget.update", "widget.message", "widget.dispose",
        "widget.api_call", "widget.api_response", "widget.api_cancel",
        "widget.mounted", "widget.error", "widget.disposed",
    ):
        assert shape in text, f"contract missing {shape!r}"


def test_contract_lists_widget_error_codes():
    text = CONTRACT.read_text(encoding="utf-8")
    for code in ("4101", "4102", "4103", "4104", "4106", "4107", "5101", "5102", "5103"):
        assert code in text, f"contract missing error code {code}"


def test_contract_states_response_size_cap():
    text = CONTRACT.read_text(encoding="utf-8")
    assert "32 KiB" in text or "32 kib" in text.lower() or "32768" in text


def test_contract_states_card_id_format():
    text = CONTRACT.read_text(encoding="utf-8")
    assert "wgt_" in text
