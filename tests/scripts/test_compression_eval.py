"""Unit tests for scripts/compression_eval/ non-LLM paths.

These exercise rubric parsing, report rendering, and fixture/probe
loading — everything that does NOT require API credentials. The eval
harness itself (run_eval.py) is not hermetic and is not tested here.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts" / "compression_eval"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from rubric import (  # noqa: E402
    DIMENSIONS,
    SCORE_SCALE,
    build_judge_prompt,
    parse_judge_response,
)
from report import (  # noqa: E402
    render_report,
    summarize_fixture_runs,
    write_run_json,
)


# ---------- rubric.parse_judge_response ----------


def test_parse_judge_response_accepts_clean_json():
    raw = json.dumps({
        "accuracy": 4,
        "context_awareness": 3,
        "artifact_trail": 2,
        "completeness": 5,
        "continuity": 4,
        "instruction_following": 5,
        "notes": "missed redis_client.py",
    })
    out = parse_judge_response(raw)
    assert out["scores"]["accuracy"] == 4
    assert out["scores"]["artifact_trail"] == 2
    assert out["notes"] == "missed redis_client.py"
    assert 0 <= out["overall"] <= 5
    # overall is the arithmetic mean of the six dims
    expected = (4 + 3 + 2 + 5 + 4 + 5) / 6
    assert abs(out["overall"] - expected) < 1e-9


def test_parse_judge_response_strips_code_fences():
    raw = '```json\n{"accuracy":5,"context_awareness":5,"artifact_trail":5,"completeness":5,"continuity":5,"instruction_following":5,"notes":""}\n```'
    out = parse_judge_response(raw)
    assert all(v == 5 for v in out["scores"].values())


def test_parse_judge_response_tolerates_surrounding_prose():
    raw = (
        "Here is my grading:\n\n"
        '{"accuracy": 3, "context_awareness": 4, "artifact_trail": 3, '
        '"completeness": 4, "continuity": 3, "instruction_following": 5, '
        '"notes": "ok"}\n\n'
        "Let me know if you need more detail."
    )
    out = parse_judge_response(raw)
    assert out["scores"]["accuracy"] == 3


def test_parse_judge_response_rounds_floats_to_ints():
    raw = json.dumps({
        "accuracy": 3.4,
        "context_awareness": 3.6,
        "artifact_trail": 3,
        "completeness": 3,
        "continuity": 3,
        "instruction_following": 3,
        "notes": "",
    })
    out = parse_judge_response(raw)
    assert out["scores"]["accuracy"] == 3
    assert out["scores"]["context_awareness"] == 4


def test_parse_judge_response_rejects_out_of_range():
    raw = json.dumps({
        "accuracy": 7,  # illegal
        "context_awareness": 3, "artifact_trail": 3, "completeness": 3,
        "continuity": 3, "instruction_following": 3, "notes": "",
    })
    with pytest.raises(ValueError, match="out of range"):
        parse_judge_response(raw)


def test_parse_judge_response_rejects_missing_dimension():
    raw = json.dumps({
        "accuracy": 3, "context_awareness": 3, "artifact_trail": 3,
        "completeness": 3, "continuity": 3,
        # instruction_following missing
        "notes": "",
    })
    with pytest.raises(ValueError, match="missing dimension"):
        parse_judge_response(raw)


def test_parse_judge_response_rejects_non_numeric():
    raw = json.dumps({
        "accuracy": "high",
        "context_awareness": 3, "artifact_trail": 3, "completeness": 3,
        "continuity": 3, "instruction_following": 3, "notes": "",
    })
    with pytest.raises(ValueError, match="not numeric"):
        parse_judge_response(raw)


def test_parse_judge_response_rejects_booleans_as_numeric():
    # JSON bools coerce to int otherwise — catch that explicitly
    raw = json.dumps({
        "accuracy": True,
        "context_awareness": 3, "artifact_trail": 3, "completeness": 3,
        "continuity": 3, "instruction_following": 3, "notes": "",
    })
    with pytest.raises(ValueError, match="not numeric"):
        parse_judge_response(raw)


def test_parse_judge_response_rejects_empty():
    with pytest.raises(ValueError, match="empty"):
        parse_judge_response("")


def test_parse_judge_response_rejects_no_json():
    with pytest.raises(ValueError, match="no JSON object"):
        parse_judge_response("just some prose with no braces at all")


def test_parse_judge_response_rejects_malformed_json():
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_judge_response("{accuracy: 3,}")  # missing quotes, trailing comma


def test_parse_judge_response_truncates_long_notes():
    long_notes = "x" * 500
    raw = json.dumps({
        "accuracy": 3, "context_awareness": 3, "artifact_trail": 3,
        "completeness": 3, "continuity": 3, "instruction_following": 3,
        "notes": long_notes,
    })
    out = parse_judge_response(raw)
    assert len(out["notes"]) == 200


# ---------- rubric.build_judge_prompt ----------


def test_build_judge_prompt_mentions_all_dimensions():
    prompt = build_judge_prompt(
        probe_question="What files were modified?",
        probe_type="artifact",
        expected_facts=["foo.py", "bar.py"],
        assistant_answer="I modified foo.py.",
    )
    for dim in DIMENSIONS:
        assert dim in prompt


def test_build_judge_prompt_includes_expected_facts():
    prompt = build_judge_prompt(
        probe_question="What files were modified?",
        probe_type="artifact",
        expected_facts=["specific_file.py", "another_file.py"],
        assistant_answer="n/a",
    )
    assert "specific_file.py" in prompt
    assert "another_file.py" in prompt


def test_build_judge_prompt_handles_empty_expected_facts():
    prompt = build_judge_prompt(
        probe_question="anything?",
        probe_type="recall",
        expected_facts=[],
        assistant_answer="nope",
    )
    assert "(none provided)" in prompt


def test_build_judge_prompt_includes_all_score_scale_levels():
    prompt = build_judge_prompt(
        probe_question="q", probe_type="recall",
        expected_facts=[], assistant_answer="a",
    )
    for score in SCORE_SCALE:
        assert f"  {score}:" in prompt


# ---------- report.summarize_fixture_runs ----------


def _fake_run(fixture_name: str, run_index: int, probe_scores: dict) -> dict:
    """Build a synthetic per-run payload for summariser tests."""
    probes = []
    for pid, per_dim in probe_scores.items():
        overall = sum(per_dim.values()) / len(per_dim)
        probes.append({
            "id": pid,
            "type": "recall",
            "question": "q",
            "expected_facts": [],
            "answer": "a",
            "scores": per_dim,
            "overall": overall,
            "notes": f"note-run{run_index}",
            "parse_error": None,
            "elapsed_seconds": 0.1,
        })
    return {
        "fixture_name": fixture_name,
        "run_index": run_index,
        "compression": {
            "pre_tokens": 10000,
            "post_tokens": 5000,
            "compression_ratio": 0.5,
            "pre_message_count": 50,
            "post_message_count": 25,
            "summary_text": "## Active Task\n...",
        },
        "probes": probes,
    }


def _all_dims(value: int) -> dict:
    return {d: value for d in DIMENSIONS}


def test_summarize_handles_single_run():
    runs = [_fake_run("fx1", 1, {
        "p1": _all_dims(4),
        "p2": _all_dims(3),
    })]
    s = summarize_fixture_runs(runs)
    assert s["fixture_name"] == "fx1"
    assert s["runs"] == 1
    # Median of {4, 3} per dim is 3.5
    for d in DIMENSIONS:
        assert abs(s["dimension_medians"][d] - 3.5) < 1e-9
    # Both probes have overall >= 3.0 so no misses
    assert s["misses"] == []


def test_summarize_flags_misses_below_three():
    runs = [_fake_run("fx1", 1, {
        "p_good": _all_dims(4),
        "p_bad": _all_dims(2),
    })]
    s = summarize_fixture_runs(runs)
    miss_ids = [m["id"] for m in s["misses"]]
    assert "p_bad" in miss_ids
    assert "p_good" not in miss_ids
    miss_entry = next(m for m in s["misses"] if m["id"] == "p_bad")
    assert miss_entry["overall_median"] == 2.0
    assert miss_entry["notes"] == "note-run1"


def test_summarize_medians_across_runs():
    # Three runs, same probe, scores 2, 4, 5 per dim -> median 4
    runs = [
        _fake_run("fx1", 1, {"p": _all_dims(2)}),
        _fake_run("fx1", 2, {"p": _all_dims(4)}),
        _fake_run("fx1", 3, {"p": _all_dims(5)}),
    ]
    s = summarize_fixture_runs(runs)
    for d in DIMENSIONS:
        assert s["dimension_medians"][d] == 4.0
    assert s["runs"] == 3


def test_summarize_empty_input():
    assert summarize_fixture_runs([]) == {}


# ---------- report.render_report ----------


def test_render_report_renders_all_fixtures():
    runs = [_fake_run("feature-impl", 1, {"p1": _all_dims(4)})]
    s = summarize_fixture_runs(runs)
    md = render_report(
        label="test",
        compressor_model="modelA",
        judge_model="modelA",
        runs_per_fixture=1,
        summaries=[s],
    )
    assert "feature-impl" in md
    assert "modelA" in md
    for dim in DIMENSIONS:
        assert dim in md
    # Methodology footer present
    assert "Methodology" in md
    assert "factory.ai" in md


def test_render_report_shows_deltas_when_baseline_provided():
    baseline_runs = [_fake_run("fx", 1, {"p1": _all_dims(3)})]
    current_runs = [_fake_run("fx", 1, {"p1": _all_dims(4)})]
    baseline = [summarize_fixture_runs(baseline_runs)]
    current = [summarize_fixture_runs(current_runs)]
    md = render_report(
        label="test",
        compressor_model="m",
        judge_model="m",
        runs_per_fixture=1,
        summaries=current,
        baseline_summaries=baseline,
    )
    # Improvement of +1 from 3 -> 4 on every dim
    assert "+1.00" in md
    assert "Deltas shown against baseline" in md


def test_render_report_lists_misses_section():
    runs = [_fake_run("fx", 1, {
        "good": _all_dims(4),
        "bad": _all_dims(1),
    })]
    s = summarize_fixture_runs(runs)
    md = render_report(
        label="t", compressor_model="m", judge_model="m",
        runs_per_fixture=1, summaries=[s],
    )
    assert "Probes scoring below 3.0" in md
    assert "`bad`" in md
    assert "`good`" not in md


def test_render_report_no_misses_section_when_all_pass():
    runs = [_fake_run("fx", 1, {"p": _all_dims(5)})]
    s = summarize_fixture_runs(runs)
    md = render_report(
        label="t", compressor_model="m", judge_model="m",
        runs_per_fixture=1, summaries=[s],
    )
    assert "Probes scoring below 3.0" not in md


def test_render_report_compression_table():
    runs = [_fake_run("fx", 1, {"p": _all_dims(4)})]
    s = summarize_fixture_runs(runs)
    md = render_report(
        label="t", compressor_model="m", judge_model="m",
        runs_per_fixture=1, summaries=[s],
    )
    assert "Pre tokens" in md
    assert "10000" in md  # from _fake_run compression.pre_tokens
    assert "50.0%" in md  # ratio renders as percent


# ---------- report.write_run_json ----------


def test_write_run_json_roundtrip(tmp_path):
    payload = _fake_run("fx1", 2, {"p": _all_dims(4)})
    out = write_run_json(
        results_dir=tmp_path,
        fixture_name="fx1",
        run_index=2,
        payload=payload,
    )
    assert out.exists()
    assert out.name == "fx1-run-2.json"
    with out.open() as fh:
        loaded = json.load(fh)
    assert loaded["fixture_name"] == "fx1"
    assert loaded["run_index"] == 2


# ---------- fixture + probe sanity ----------


_EVAL_DIR = Path(__file__).resolve().parents[2] / "scripts" / "compression_eval"


@pytest.mark.parametrize("fixture_name", [
    "feature-impl-context-priority",
    "debug-session-feishu-id-model",
    "config-build-competitive-scouts",
])
def test_fixture_loads_and_is_well_formed(fixture_name):
    path = _EVAL_DIR / "fixtures" / f"{fixture_name}.json"
    assert path.exists(), f"fixture missing: {path}"
    with path.open() as fh:
        fx = json.load(fh)
    assert fx["name"] == fixture_name
    assert isinstance(fx["messages"], list) and len(fx["messages"]) > 10
    assert fx["messages"][0]["role"] == "system"
    # At least one user message and one assistant message
    roles = {m["role"] for m in fx["messages"]}
    assert "user" in roles and "assistant" in roles


@pytest.mark.parametrize("fixture_name", [
    "feature-impl-context-priority",
    "debug-session-feishu-id-model",
    "config-build-competitive-scouts",
])
def test_probes_have_all_four_types(fixture_name):
    path = _EVAL_DIR / "probes" / f"{fixture_name}.probes.json"
    assert path.exists(), f"probe bank missing: {path}"
    with path.open() as fh:
        pb = json.load(fh)
    assert pb["fixture"] == fixture_name
    types = {p["type"] for p in pb["probes"]}
    assert types == {"recall", "artifact", "continuation", "decision"}, (
        f"{fixture_name} probe bank missing at least one probe type; got {types}"
    )
    # Every probe has expected_facts (possibly empty list but present)
    for p in pb["probes"]:
        assert "id" in p and "question" in p and "type" in p
        assert "expected_facts" in p and isinstance(p["expected_facts"], list)


def test_fixtures_do_not_leak_maintainer_pii():
    """Smoke test that scrubber actually ran. This is a belt-and-suspenders
    check that would have caught the ethanbit@qq.com leak before it
    landed."""
    for fixture_path in (_EVAL_DIR / "fixtures").glob("*.json"):
        text = fixture_path.read_text()
        lower = text.lower()
        # The scrubbing_passes metadata intentionally documents what was
        # replaced. Ignore the metadata block and only scan the messages.
        data = json.loads(text)
        msg_text = json.dumps(data["messages"])
        msg_lower = msg_text.lower()
        assert "teknium" not in msg_lower, (
            f"{fixture_path.name}: maintainer handle leaked into messages"
        )
        # No personal-email domains (placeholder @example.com is allowed)
        import re
        personal_emails = re.findall(
            r"[A-Za-z0-9._%+-]+@(?!example\.com)[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
            msg_text,
        )
        assert personal_emails == [], (
            f"{fixture_path.name}: personal email(s) leaked: {personal_emails}"
        )
