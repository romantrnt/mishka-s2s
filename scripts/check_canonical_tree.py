#!/usr/bin/env python3
"""Reject isolated historical *final* snapshots in canonical production roots."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def is_isolated_final(path: Path) -> bool:
    name = path.name.lower()
    return (name.endswith(".py") and "-final-" in name) or (
        ".py." in name and "final" in name
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root = args.root.resolve()
    candidates = []
    for relative in ("app", "scripts"):
        directory = root / relative
        if not directory.is_dir():
            continue
        candidates.extend(
            path for path in directory.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
            and ".ipynb_checkpoints" not in path.parts
            and is_isolated_final(path)
        )
    if candidates:
        print("isolated final snapshots found in canonical production roots:")
        for path in sorted(candidates):
            print(path)
        return 1
    print("canonical source-tree final-snapshot check: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
