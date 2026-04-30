"""list_widget_examples / read_widget_example: discovery + read of starter .tsx files."""

import json
from pathlib import Path

from tools import widget_tools  # noqa: F401  triggers registration
from tools.registry import registry


REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_DIR = REPO_ROOT / "assets" / "widget_prompts" / "examples"


def _list_call():
    return json.loads(registry.get_entry("list_widget_examples").handler({}))


def _read_call(name):
    return json.loads(registry.get_entry("read_widget_example").handler({"name": name}))


def test_list_returns_each_starter_example():
    payload = _list_call()
    names = {item["name"] for item in payload["examples"]}
    assert {"static-info", "form-with-hermes-ask", "list-with-storage", "chart"}.issubset(names)


def test_list_extracts_summary_from_first_jsdoc_line():
    payload = _list_call()
    item = next(i for i in payload["examples"] if i["name"] == "static-info")
    assert "Static info card" in item["summary"] or "presentational" in item["summary"].lower()


def test_list_summaries_are_one_line():
    payload = _list_call()
    for item in payload["examples"]:
        assert "\n" not in item["summary"]
        assert len(item["summary"]) <= 200


def test_read_returns_full_file_content():
    payload = _read_call("static-info")
    assert "export default" in payload["content"]
    assert payload["name"] == "static-info"


def test_read_rejects_unknown_name():
    payload = _read_call("does-not-exist")
    assert "error" in payload
    assert payload["error"]["code"] == 4001 or "not_found" in payload["error"].get("message", "").lower() or payload["error"].get("kind") == "not_found"


def test_read_rejects_path_traversal():
    payload = _read_call("../../../etc/passwd")
    assert "error" in payload


def test_read_rejects_invalid_name_chars():
    payload = _read_call("name with spaces")
    assert "error" in payload


def test_read_includes_jsdoc_block():
    """Reading an example returns the JSDoc — that's the whole point of
    fetching examples on demand. The agent reads the doc to learn the
    pattern's capability declaration."""
    payload = _read_call("form-with-hermes-ask")
    assert "/**" in payload["content"]
    assert "Capabilities" in payload["content"]


def test_list_handles_empty_directory(tmp_path, monkeypatch):
    """If the examples dir is empty, list returns []."""
    import tools.widget_tools as wt
    monkeypatch.setattr(wt, "EXAMPLES_DIR", tmp_path)
    payload = _list_call()
    assert payload == {"examples": []}


def test_list_skips_non_tsx_files(tmp_path, monkeypatch):
    """Only .tsx files are listed. README.md, .gitkeep, etc. are ignored."""
    import tools.widget_tools as wt
    (tmp_path / "ignored.md").write_text("README content")
    (tmp_path / ".gitkeep").write_text("")
    (tmp_path / "real.tsx").write_text("/**\n * Real example.\n */\nexport default function() {}")
    monkeypatch.setattr(wt, "EXAMPLES_DIR", tmp_path)
    payload = _list_call()
    assert {item["name"] for item in payload["examples"]} == {"real"}
