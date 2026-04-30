#!/usr/bin/env python3
"""Sync widget example .tsx files from the Tauri-side contracts pipeline.

The Tauri-side contracts/examples/ directory is the source of truth.
Hermes-side assets/widget_prompts/examples/ mirrors it; this script
copies new and changed files, reports orphans, and is idempotent.

Usage:
    scripts/sync_widget_examples.py --source ~/projects/anandia-workspace/contracts/examples
    scripts/sync_widget_examples.py --source <path> --dry-run
    scripts/sync_widget_examples.py --source <path> --target assets/widget_prompts/examples
"""

from __future__ import annotations

import argparse
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
