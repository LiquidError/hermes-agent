"""sync_widget_examples.py — copies .tsx from source to target, idempotent, reports drift."""

import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "sync_widget_examples.py"


def _run_sync(*, source: Path, target: Path, extra_args=()):
    cmd = [
        sys.executable, str(SCRIPT),
        "--source", str(source),
        "--target", str(target),
        *extra_args,
    ]
    return subprocess.run(cmd, capture_output=True, text=True)


def test_copies_tsx_files_from_source_to_target(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.tsx").write_text("export default function A(){return null}")
    (src / "b.tsx").write_text("export default function B(){return null}")
    tgt = tmp_path / "tgt"
    tgt.mkdir()

    result = _run_sync(source=src, target=tgt)
    assert result.returncode == 0, result.stderr
    assert (tgt / "a.tsx").read_text() == "export default function A(){return null}"
    assert (tgt / "b.tsx").read_text() == "export default function B(){return null}"


def test_skips_non_tsx_files(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.tsx").write_text("a")
    (src / "README.md").write_text("readme")
    (src / "package.json").write_text("{}")
    tgt = tmp_path / "tgt"
    tgt.mkdir()

    _run_sync(source=src, target=tgt)
    assert (tgt / "a.tsx").exists()
    assert not (tgt / "README.md").exists()
    assert not (tgt / "package.json").exists()


def test_is_idempotent_when_target_already_matches(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.tsx").write_text("hello")
    tgt = tmp_path / "tgt"
    tgt.mkdir()
    (tgt / "a.tsx").write_text("hello")

    before_mtime = (tgt / "a.tsx").stat().st_mtime_ns
    result = _run_sync(source=src, target=tgt)
    after_mtime = (tgt / "a.tsx").stat().st_mtime_ns
    assert result.returncode == 0
    assert before_mtime == after_mtime


def test_reports_drift_summary(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.tsx").write_text("new")
    (src / "b.tsx").write_text("added")
    tgt = tmp_path / "tgt"
    tgt.mkdir()
    (tgt / "a.tsx").write_text("old")
    (tgt / "c.tsx").write_text("orphan")

    result = _run_sync(source=src, target=tgt)
    assert result.returncode == 0
    out = result.stdout
    assert "a.tsx" in out and "updated" in out.lower()
    assert "b.tsx" in out and ("added" in out.lower() or "new" in out.lower())
    assert "c.tsx" in out and "orphan" in out.lower()


def test_dry_run_does_not_write(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.tsx").write_text("new")
    tgt = tmp_path / "tgt"
    tgt.mkdir()

    result = _run_sync(source=src, target=tgt, extra_args=("--dry-run",))
    assert result.returncode == 0
    assert not (tgt / "a.tsx").exists()
    assert "dry" in result.stdout.lower() or "would" in result.stdout.lower()


def test_missing_source_dir_exits_nonzero(tmp_path):
    tgt = tmp_path / "tgt"
    tgt.mkdir()
    result = _run_sync(source=tmp_path / "ghost", target=tgt)
    assert result.returncode != 0
    assert "source" in (result.stderr + result.stdout).lower()
