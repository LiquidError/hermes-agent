# Plan 05 — Example Tools, Starter Examples, Sync Script

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The agent calls `list_widget_examples()` to discover available widget patterns, picks one, and calls `read_widget_example(name)` to pull its `.tsx` source plus inline JSDoc into context. Four starter examples ship with this plan. A sync script keeps the local copy aligned with the Tauri-side source of truth.

**Architecture:** `tools/widget_tools.py` replaces the `list_widget_examples` and `read_widget_example` stubs with real handlers that read from `assets/widget_prompts/examples/`. `list_widget_examples` returns `[{name, summary}, ...]` where `summary` is the first JSDoc line of each `.tsx` file (or a fallback). `read_widget_example(name)` validates the name (alphanumeric + dashes, no path traversal) and returns the file's full UTF-8 content. The sync script walks the Tauri repo's `contracts/examples/` directory and copies each `.tsx` file into `assets/widget_prompts/examples/`, reporting drift.

**Tech Stack:** Python 3.11, `pathlib`, `re` for the JSDoc summary line, `argparse` for the sync CLI, pytest via `scripts/run_tests.sh`. No new runtime dependencies.

---

## File structure

**Create:**
- `assets/widget_prompts/examples/static-info.tsx` — purely presentational card.
- `assets/widget_prompts/examples/form-with-hermes-ask.tsx` — form that calls `canvasAPI.hermes.ask`.
- `assets/widget_prompts/examples/list-with-storage.tsx` — list with persisted state.
- `assets/widget_prompts/examples/chart.tsx` — `<Chart>` over agent-provided data.
- `scripts/sync_widget_examples.py` — manual-run sync script.
- `tests/tools/test_widget_examples.py` — `list_widget_examples` and `read_widget_example` integration.
- `tests/scripts/test_sync_widget_examples.py` — sync script behavior.

**Modify:**
- `tools/widget_tools.py` — replace the two stubs with real handlers.

---

## Task 1: Author the four starter examples

**Files:**
- Create: `assets/widget_prompts/examples/static-info.tsx`
- Create: `assets/widget_prompts/examples/form-with-hermes-ask.tsx`
- Create: `assets/widget_prompts/examples/list-with-storage.tsx`
- Create: `assets/widget_prompts/examples/chart.tsx`

Each example MUST:
- Begin with a JSDoc block whose first line is a one-sentence summary; this is what `list_widget_examples` extracts.
- Export a default React component.
- Use only the `canvasAPI` and `canvas-primitives` surface declared in the JSDoc — no `fetch`, no CDN, no third-party imports.
- Be self-contained and runnable against the Tauri-side bootstrap.

The four files act as the canonical patterns the agent learns from. They are also referenced by the Tauri-side contracts pipeline; if the Tauri-side `contracts/examples/` already has these files, the sync script in Task 4 picks them up. For now, author them directly here so Plan 05 can ship without coordination.

- [ ] **Step 1: Author `static-info.tsx`**

```tsx
// assets/widget_prompts/examples/static-info.tsx
/**
 * Static info card with no capabilities.
 *
 * Pattern: presentational. The card renders content and never reaches back
 * to the host. Good for summaries, status snapshots, and "here is the answer"
 * artifacts the user is meant to read but not interact with.
 *
 * Capabilities: [] (none — purely visual)
 * Imports: React; Card, Stack, Text, Field from 'canvas-primitives'.
 */

import React from 'react';
import { Card, Stack, Text, Field } from 'canvas-primitives';

export default function StaticInfoCard() {
  return (
    <Card title="Quarterly summary">
      <Stack gap={12}>
        <Field label="Quarter">Q3 2025</Field>
        <Field label="Revenue">$4.2M</Field>
        <Field label="Growth">+18% YoY</Field>
        <Text muted>
          Generated from the closing financial pack on 2025-10-15.
        </Text>
      </Stack>
    </Card>
  );
}
```

- [ ] **Step 2: Author `form-with-hermes-ask.tsx`**

```tsx
// assets/widget_prompts/examples/form-with-hermes-ask.tsx
/**
 * Form with a "fill in" button that calls hermes.ask to populate fields.
 *
 * Pattern: round-trip. The card collects user input AND can ask the agent
 * to fill known fields. Demonstrates the canvasAPI.hermes.ask round-trip
 * with the async accept/correlate/respond flow handled invisibly by the
 * Tauri broker.
 *
 * Capabilities: ['hermes.ask', 'notes.save']
 * Imports: React (with hooks); Card, Field, Button, Stack from 'canvas-primitives'.
 */

import React, { useState, useCallback } from 'react';
import { Card, Field, Button, Stack, Text } from 'canvas-primitives';

declare const canvasAPI: {
  hermes: { ask(prompt: string): Promise<string> };
  notes: { save(args: { title: string; body: string; tags?: string[] }): Promise<{ note_id: string }> };
};

export default function RetroForm() {
  const [wins, setWins] = useState('');
  const [misses, setMisses] = useState('');
  const [busy, setBusy] = useState(false);

  const fillFromAgent = useCallback(async () => {
    setBusy(true);
    try {
      const summary = await canvasAPI.hermes.ask(
        'Fill in known wins and misses from this quarter as bullet lists.'
      );
      // The card decides how to parse the answer — here we split on a
      // separator the prompt asks for.
      const [w, m] = summary.split('\n---\n');
      if (w) setWins(w);
      if (m) setMisses(m);
    } finally {
      setBusy(false);
    }
  }, []);

  const save = useCallback(async () => {
    await canvasAPI.notes.save({
      title: 'Q3 retro',
      body: `## Wins\n${wins}\n\n## Misses\n${misses}`,
      tags: ['retro', 'quarterly'],
    });
  }, [wins, misses]);

  return (
    <Card title="Q3 retro">
      <Stack gap={12}>
        <Field label="Wins">
          <textarea value={wins} onChange={(e) => setWins(e.target.value)} rows={6} />
        </Field>
        <Field label="Misses">
          <textarea value={misses} onChange={(e) => setMisses(e.target.value)} rows={6} />
        </Field>
        <Stack gap={8} direction="row">
          <Button onClick={fillFromAgent} disabled={busy}>
            {busy ? 'Asking…' : 'Fill from agent'}
          </Button>
          <Button onClick={save} primary>Save as note</Button>
        </Stack>
        {busy && <Text muted>Hermes is thinking…</Text>}
      </Stack>
    </Card>
  );
}
```

- [ ] **Step 3: Author `list-with-storage.tsx`**

```tsx
// assets/widget_prompts/examples/list-with-storage.tsx
/**
 * Reorderable list with per-card persistent state.
 *
 * Pattern: stateful + persisted. The card maintains its own list of items
 * across re-mounts via canvasAPI.storage. Good for trackers, todos,
 * configurations, and anything the user expects to find unchanged when
 * they come back.
 *
 * Capabilities: ['storage.get', 'storage.set']
 * Imports: React (with hooks); Card, Stack, Button, Text, Row from 'canvas-primitives'.
 */

import React, { useEffect, useState } from 'react';
import { Card, Stack, Button, Text, Row } from 'canvas-primitives';

declare const canvasAPI: {
  storage: {
    get(key: string): Promise<unknown>;
    set(key: string, value: unknown): Promise<void>;
  };
};

const KEY = 'tracker.items';

export default function TrackerCard() {
  const [items, setItems] = useState<string[]>([]);
  const [draft, setDraft] = useState('');
  const [hydrated, setHydrated] = useState(false);

  useEffect(() => {
    canvasAPI.storage.get(KEY).then((value) => {
      if (Array.isArray(value)) setItems(value as string[]);
      setHydrated(true);
    });
  }, []);

  const persist = (next: string[]) => {
    setItems(next);
    canvasAPI.storage.set(KEY, next);
  };

  const add = () => {
    if (!draft.trim()) return;
    persist([...items, draft.trim()]);
    setDraft('');
  };

  const remove = (i: number) => persist(items.filter((_, j) => j !== i));

  if (!hydrated) return <Card title="Tracker"><Text muted>Loading…</Text></Card>;

  return (
    <Card title="Tracker">
      <Stack gap={8}>
        {items.map((item, i) => (
          <Row key={i}>
            <Text>{item}</Text>
            <Button onClick={() => remove(i)} subtle>Remove</Button>
          </Row>
        ))}
        <Row>
          <input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="New item…"
          />
          <Button onClick={add}>Add</Button>
        </Row>
      </Stack>
    </Card>
  );
}
```

- [ ] **Step 4: Author `chart.tsx`**

```tsx
// assets/widget_prompts/examples/chart.tsx
/**
 * Chart over agent-supplied data, refreshed via widget.message.
 *
 * Pattern: data-driven, agent-pushed updates. The card renders a chart of
 * whatever data the agent pushes via widget_message. Good for dashboards,
 * comparisons, time series, and any case where the agent has already
 * computed a structured dataset and just needs to display it.
 *
 * Capabilities: [] (the agent pushes data via widget_message; the card
 *                  doesn't need to call back)
 * Imports: React (with hooks); Card, Chart from 'canvas-primitives';
 *          canvasAPI.onMessage for receiving structured pushes.
 */

import React, { useEffect, useState } from 'react';
import { Card, Chart, Text } from 'canvas-primitives';

declare const canvasAPI: {
  onMessage(handler: (msg: unknown) => void): () => void;
};

type DataPoint = { label: string; value: number };

export default function DataChart() {
  const [data, setData] = useState<DataPoint[]>([]);
  const [title, setTitle] = useState<string>('Chart');

  useEffect(() => {
    return canvasAPI.onMessage((msg) => {
      const m = msg as { kind?: string; data?: DataPoint[]; title?: string };
      if (m.kind === 'data.refresh' && Array.isArray(m.data)) {
        setData(m.data);
        if (typeof m.title === 'string') setTitle(m.title);
      }
    });
  }, []);

  if (data.length === 0) {
    return <Card title={title}><Text muted>Awaiting data…</Text></Card>;
  }

  return (
    <Card title={title}>
      <Chart data={data} kind="bar" />
    </Card>
  );
}
```

- [ ] **Step 5: Commit**

```
git add assets/widget_prompts/examples/
git commit -m "feat(prompts): add four starter widget examples (info, form, list, chart)"
```

---

## Task 2: `list_widget_examples` real implementation

**Files:**
- Modify: `tools/widget_tools.py`
- Test: `tests/tools/test_widget_examples.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/tools/test_widget_examples.py
"""list_widget_examples / read_widget_example: discovery + read of starter .tsx files."""

import json
import os
import shutil
from pathlib import Path

import pytest

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
```

- [ ] **Step 2: Run tests to verify they fail**

```
scripts/run_tests.sh tests/tools/test_widget_examples.py -v
```

Expected: FAIL — `list_widget_examples` and `read_widget_example` are still stubs.

- [ ] **Step 3: Implement the two handlers**

In `tools/widget_tools.py`:

```python
import re
from pathlib import Path

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "assets" / "widget_prompts" / "examples"

# Names are alphanumeric + dashes; no slashes, no dots, no traversal.
_VALID_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")

# JSDoc summary extraction. The summary is the first non-blank textual line
# inside the leading /** ... */ block, after stripping leading "*" and whitespace.
_JSDOC_OPEN_RE = re.compile(r"^\s*/\*\*\s*$")
_JSDOC_LINE_STRIP_RE = re.compile(r"^\s*\*\s?")


def _extract_summary(text: str) -> str:
    """Pull the first line of the leading JSDoc block, or fall back to the filename."""
    lines = text.splitlines()
    in_block = False
    for line in lines:
        if not in_block:
            if _JSDOC_OPEN_RE.match(line):
                in_block = True
            continue
        # Inside the block. Skip blank lines and the closing */.
        stripped = _JSDOC_LINE_STRIP_RE.sub("", line).rstrip()
        if not stripped or stripped == "/":
            continue
        if "*/" in stripped:
            break
        return stripped[:200]
    return ""


def _list_widget_examples(args: dict, **kwargs: Any) -> str:
    if not EXAMPLES_DIR.is_dir():
        return json.dumps({"examples": []}, ensure_ascii=False)

    items = []
    for path in sorted(EXAMPLES_DIR.iterdir()):
        if path.suffix != ".tsx":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        summary = _extract_summary(text) or f"{path.stem} example"
        items.append({"name": path.stem, "summary": summary})

    return json.dumps({"examples": items}, ensure_ascii=False)


def _read_widget_example(args: dict, **kwargs: Any) -> str:
    name = (args.get("name") or "").strip()
    if not name or not _VALID_NAME_RE.match(name):
        return _err(4012, f"invalid example name: {name!r}", kind="invalid_name")

    path = EXAMPLES_DIR / f"{name}.tsx"
    # Defense in depth — make sure the resolved path is inside EXAMPLES_DIR.
    try:
        resolved = path.resolve()
        resolved.relative_to(EXAMPLES_DIR.resolve())
    except (OSError, ValueError):
        return _err(4012, f"invalid example path: {name!r}", kind="invalid_path")

    if not resolved.is_file():
        return _err(4001, f"example not found: {name!r}", kind="not_found")

    try:
        content = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        return _err(5001, f"failed to read example: {exc}", kind="io_error")

    return json.dumps({"name": name, "content": content}, ensure_ascii=False)
```

Wire both into `_handler_for`:

```python
def _handler_for(name: str):
    return {
        "render_widget": _render_widget,
        "widget_update": _widget_update,
        "widget_message": _widget_message,
        "widget_dispose": _widget_dispose,
        "list_widget_examples": _list_widget_examples,
        "read_widget_example": _read_widget_example,
    }.get(name) or (lambda args, _tname=name, **kw: _stub(_tname))
```

- [ ] **Step 4: Run tests to verify they pass**

```
scripts/run_tests.sh tests/tools/test_widget_examples.py -v
```

Expected: 9 passed.

- [ ] **Step 5: Commit**

```
git add tools/widget_tools.py tests/tools/test_widget_examples.py
git commit -m "feat(tools): real list_widget_examples and read_widget_example with path-traversal guards"
```

---

## Task 3: Sync script `scripts/sync_widget_examples.py`

**Files:**
- Create: `scripts/sync_widget_examples.py`
- Test: `tests/scripts/test_sync_widget_examples.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/scripts/test_sync_widget_examples.py
"""sync_widget_examples.py: copies .tsx files from Tauri-side contracts/examples into assets/widget_prompts/examples.

Idempotent: re-running with no upstream changes is a no-op. Drift is reported on completion.
"""

import io
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


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
    # Identical content shouldn't be rewritten — mtime unchanged.
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
```

- [ ] **Step 2: Run tests to verify they fail**

```
scripts/run_tests.sh tests/scripts/test_sync_widget_examples.py -v
```

Expected: FAIL — script does not exist.

- [ ] **Step 3: Implement the script**

```python
# scripts/sync_widget_examples.py
#!/usr/bin/env python3
"""Sync widget example .tsx files from the Tauri-side contracts pipeline.

The Tauri-side contracts/examples/ directory is the source of truth.
Hermes-side assets/widget_prompts/examples/ mirrors it; this script
copies new and changed files, reports orphans, and is idempotent.

Usage:
    scripts/sync_widget_examples.py --source ~/projects/anandia-workspace/contracts/examples
    scripts/sync_widget_examples.py --source <path> --dry-run
    scripts/sync_widget_examples.py --source <path> --target assets/widget_prompts/examples

If --target is omitted, defaults to <repo>/assets/widget_prompts/examples relative
to the script's location.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path


def _default_target() -> Path:
    return Path(__file__).resolve().parent.parent / "assets" / "widget_prompts" / "examples"


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sync widget example .tsx files from the Tauri side.")
    p.add_argument("--source", required=True, type=Path,
                   help="Path to the Tauri-side contracts/examples directory.")
    p.add_argument("--target", type=Path, default=None,
                   help="Path to assets/widget_prompts/examples (defaults to repo location).")
    p.add_argument("--dry-run", action="store_true",
                   help="Report changes without writing.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    source = args.source
    target = args.target or _default_target()
    dry_run = args.dry_run

    if not source.is_dir():
        print(f"error: source directory does not exist: {source}", file=sys.stderr)
        return 2

    target.mkdir(parents=True, exist_ok=True)

    src_files = {p.name: p for p in source.iterdir() if p.suffix == ".tsx" and p.is_file()}
    tgt_files = {p.name: p for p in target.iterdir() if p.suffix == ".tsx" and p.is_file()}

    added: list[str] = []
    updated: list[str] = []
    unchanged: list[str] = []
    orphans: list[str] = []

    for name, src_path in sorted(src_files.items()):
        tgt_path = target / name
        src_text = src_path.read_text(encoding="utf-8")
        if not tgt_path.exists():
            if not dry_run:
                tgt_path.write_text(src_text, encoding="utf-8")
            added.append(name)
            continue
        tgt_text = tgt_path.read_text(encoding="utf-8")
        if tgt_text == src_text:
            unchanged.append(name)
            continue
        if not dry_run:
            tgt_path.write_text(src_text, encoding="utf-8")
        updated.append(name)

    for name in sorted(tgt_files):
        if name not in src_files:
            orphans.append(name)

    print(f"Source: {source}")
    print(f"Target: {target}")
    if dry_run:
        print("(dry-run — no files written; would-do summary:)")
    print(f"  added:     {len(added)}  {' '.join(added) if added else ''}")
    print(f"  updated:   {len(updated)}  {' '.join(updated) if updated else ''}")
    print(f"  unchanged: {len(unchanged)}")
    if orphans:
        print(f"  orphans (in target, not in source): {len(orphans)}  {' '.join(orphans)}")
        print(f"    — orphans are NOT removed automatically. Remove them by hand if intended.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Make it executable**

```
chmod +x scripts/sync_widget_examples.py
```

- [ ] **Step 5: Run tests to verify they pass**

```
scripts/run_tests.sh tests/scripts/test_sync_widget_examples.py -v
```

Expected: 6 passed.

- [ ] **Step 6: Commit**

```
git add scripts/sync_widget_examples.py tests/scripts/test_sync_widget_examples.py
git commit -m "feat(scripts): add idempotent sync for widget examples from the Tauri side"
```

---

## Task 4: End-to-end smoke — agent calls list, reads one, content survives

**Files:**
- Test: `tests/tools/test_widget_examples_e2e.py`

- [ ] **Step 1: Add the test**

```python
# tests/tools/test_widget_examples_e2e.py
"""End-to-end: agent calls list_widget_examples, picks one, reads it back.

This is the workflow encoded in the addendum (assets/widget_prompts/addendum.md):
list to discover, read to learn the pattern, then render.
"""

import json

from tools.registry import registry


def test_list_then_read_workflow():
    list_handler = registry.get_entry("list_widget_examples").handler
    listed = json.loads(list_handler({}))
    assert listed["examples"], "no starter examples shipped"

    read_handler = registry.get_entry("read_widget_example").handler
    for item in listed["examples"]:
        result = json.loads(read_handler({"name": item["name"]}))
        assert "content" in result, f"failed to read {item['name']}: {result!r}"
        # Each example contains a JSDoc with a Capabilities line so the agent
        # knows what to declare in render_widget.
        assert "Capabilities" in result["content"]
```

- [ ] **Step 2: Run the test**

```
scripts/run_tests.sh tests/tools/test_widget_examples_e2e.py -v
```

Expected: PASS.

- [ ] **Step 3: Commit**

```
git add tests/tools/test_widget_examples_e2e.py
git commit -m "test(tools): list-then-read workflow over the four starter examples"
```

---

## Task 5: Confirm addendum-references-tools alignment

The addendum (shipped in Plan 01) tells the agent to call `list_widget_examples()` then `read_widget_example(name)` before rendering. This task is a small cross-doc sanity test so a future addendum edit doesn't drift away from the tool surface.

**Files:**
- Test: `tests/agent/test_addendum_references_tools.py`

- [ ] **Step 1: Add the test**

```python
# tests/agent/test_addendum_references_tools.py
"""The widget addendum mentions the discovery tools the agent must call.

Edit guard: if a future tweak removes "list_widget_examples" or
"read_widget_example" from the addendum, the agent will silently stop
calling them and start writing widgets blind to the primitives surface.
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
```

- [ ] **Step 2: Run the test**

```
scripts/run_tests.sh tests/agent/test_addendum_references_tools.py -v
```

Expected: 3 passed (Plan 01 already shipped the addendum referencing all six tools).

- [ ] **Step 3: Commit**

```
git add tests/agent/test_addendum_references_tools.py
git commit -m "test(agent): addendum references the six widget tools"
```

---

## Task 6: Append a Widget render section to the base wire contract

The source spec §13.11 calls for the base wire contract (`plans/tauri-client-contract.md`) to gain a "Widget render" section that mirrors `hermes-widget-render-spec.md` §3, so the canonical wire surface lives in one place going forward.

**Files:**
- Modify: `plans/tauri-client-contract.md` — append a new `§N. Widget render` section.

- [ ] **Step 1: Read the current contract structure**

```
wc -l plans/tauri-client-contract.md
```

Note the section numbering at the bottom (e.g. existing top-level §15 / §16) so the new section gets the next contiguous number.

- [ ] **Step 2: Append the Widget render section**

The new section mirrors `hermes-widget-render-spec.md` §3 verbatim, with these adaptations:

- Lead with one sentence describing what the section adds: "Wire-level surface for the widget runtime — six events server→client, one method client→server, four event-shape messages client→server. Implemented on the Tauri side per `plans/widget-runtime/`, on the Hermes side per `plans/hermes-widget-runtime/`."
- Reproduce the JSON envelopes for `widget.render`, `widget.update`, `widget.message`, `widget.dispose`, `widget.api_call`, `widget.api_response`, `widget.api_cancel`, plus the inbound `widget.mounted`, `widget.error`, `widget.disposed`.
- Reproduce the error-code table (4101–4107, 5101–5103).
- State the 32 KiB `widget.api_response` cap and the 256 KiB `widget.message` and `widget.render.source` caps.
- State the `wgt_<6 hex>` card_id format and that the server is the allocator.
- Cross-link to the two implementation specs at the top: `[hermes-widget-render-spec.md](./hermes-widget-render-spec.md)` and `[tauri-agent-widget-runtime-spec.md](./tauri-agent-widget-runtime-spec.md)`.

The section is descriptive — it doesn't add new wire shape; it consolidates what `hermes-widget-render-spec.md` §3 and `tauri-agent-widget-runtime-spec.md` §3 (where they overlap) already specified, so a future reader doesn't need to triangulate between two specs.

- [ ] **Step 3: Add a wire-contract regression test**

```python
# tests/test_wire_contract_widget_section.py
"""The base wire contract has a Widget render section that mentions every
event-type and method this plan family implements. This is an edit guard:
if someone changes the wire shape on one side without updating the other,
the contract doc surfaces it."""

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
```

- [ ] **Step 4: Run the test**

```
scripts/run_tests.sh tests/test_wire_contract_widget_section.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```
git add plans/tauri-client-contract.md tests/test_wire_contract_widget_section.py
git commit -m "docs(contract): consolidate widget runtime wire surface in base contract"
```

---

## Acceptance for Plan 05

- Four starter `.tsx` examples ship in `assets/widget_prompts/examples/`. Each leads with a JSDoc summary line and declares its capability set.
- `list_widget_examples()` returns `{"examples": [{"name", "summary"}, ...]}` with one entry per `.tsx` file. Summaries are extracted from the first JSDoc line; non-`.tsx` files are ignored.
- `read_widget_example(name)` returns `{"name", "content"}` for valid names; rejects path traversal, invalid characters, and unknown names with structured errors.
- `scripts/sync_widget_examples.py` copies `.tsx` files from a configurable Tauri-repo source path, is idempotent (unchanged files leave mtime untouched), reports added/updated/unchanged/orphan counts, supports `--dry-run`, and exits non-zero on a missing source dir.
- The addendum (Plan 01) references all six widget tools by name, validated by an alignment test.
- `plans/tauri-client-contract.md` carries a Widget render section that lists every event/method, the error-code table, the 32 KiB response cap, and the `wgt_<6 hex>` card-id format. A regression test guards against silent drift.

The agent's full widget workflow now works end-to-end:

1. The Tauri client connects with `widget.render` capability.
2. The six widget tools and the lean addendum become visible.
3. The agent calls `list_widget_examples()` → `read_widget_example("form-with-hermes-ask")` → reads the pattern.
4. The agent calls `render_widget(source=..., capabilities=["hermes.ask"])` → card mounts → `card_id` returned.
5. The card calls `canvasAPI.hermes.ask(...)` → ack → btw runs → `widget.api_response` arrives → iframe Promise resolves.
6. User closes the card → `widget.disposed` arrives → in-flight calls cancel cleanly.
7. Agent's later turn calls `widget_dispose(card_id)` → returns `{disposed: false, already_disposed: true}` → agent sees the user moved on.
