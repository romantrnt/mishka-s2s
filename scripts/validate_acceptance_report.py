
#!/usr/bin/env python3
"""Validate a saved regression/acceptance report against the evidence policy."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.foundation.acceptance_policy import validate_report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
        validate_report(report)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"acceptance report: INVALID: {exc}")
        return 1
    print("acceptance report: VALID")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

