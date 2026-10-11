#!/usr/bin/env python3
"""Secret-safe Mishka checkout provenance snapshot."""
import argparse
import hashlib
import json
import os
import platform
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

FILES = (
    "pytest.ini", "requirements-production.lock", "requirements-test.lock",
    "scripts/run_regression.py", "scripts/run_tests_nonprod.sh",
    "scripts/run_tests_production.sh", "scripts/create_provenance.py",
    "tests/test_canonical_acceptance.py", "tests/test_regression_harness.py",
    "docs/OPERATIONS.md", ".env.runpod",
)
SECRET_PARTS = ("TOKEN", "KEY", "SECRET", "PASSWORD", "COOKIE", "AUTH")


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def run(*args):
    result = subprocess.run(args, text=True, capture_output=True, check=False)
    return {"returncode": result.returncode, "stdout": result.stdout[-2000:],
            "stderr": result.stderr[-2000:]}


def runtime():
    result = {"port": 8000, "legacy_port_touched": False}
    for label, suffix in (("health", "/health"), ("ready", "/ready")):
        try:
            with urllib.request.urlopen("http://127.0.0.1:8000" + suffix, timeout=8) as response:
                payload = json.loads(response.read().decode())
            lifecycle = payload.get("lifecycle", {})
            result[label] = {
                "status": payload.get("status"), "ready": payload.get("ready"),
                "state": lifecycle.get("state"),
                "user_available": lifecycle.get("user_available"),
                "active_sessions": payload.get("active_sessions"),
                "active_tasks": payload.get("active_tasks"),
                "queued_chunks": payload.get("queued_chunks"),
                "pcm_bytes": payload.get("pcm_bytes"),
                "generation_registry": payload.get("generation_registry"),
                "audio_completion_registry": payload.get("audio_completion_registry"),
            }
        except Exception as exc:
            result[label] = {"available": False, "error_type": type(exc).__name__}
    result["processes"] = run("ps", "-eo", "pid,etime,args")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--output", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--command", action="append", default=[])
    args = parser.parse_args()
    root = Path(args.root).resolve()
    files = {}
    for name in FILES:
        path = root / name
        files[name] = {"exists": path.is_file(),
                       "bytes": path.stat().st_size if path.is_file() else None,
                       "sha256": digest(path) if path.is_file() else None}
    env_names = sorted(os.environ)
    git = {"available": False, "reason": "git_metadata_unavailable"}
    if (root / ".git").is_dir():
        git = {"available": True,
               "status": run("git", "-C", str(root), "status", "--short"),
               "commit": run("git", "-C", str(root), "rev-parse", "HEAD"),
               "branch": run("git", "-C", str(root), "branch", "--show-current"),
               "remote_names": run("git", "-C", str(root), "remote")}
    report = {
        "schema": "mishka-provenance/v1", "label": args.label,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "project_root": str(root), "git": git, "files": files,
        "host": {"platform": platform.platform(), "python": platform.python_version()},
        "environment": {
            "present_names": [n for n in env_names if not any(p in n.upper() for p in SECRET_PARTS)],
            "redacted_secret_name_presence": {n: True for n in env_names if any(p in n.upper() for p in SECRET_PARTS)},
        },
        "runtime": runtime(), "commands": args.command,
        "limitations": ["Git commit/branch/remote/diff unavailable without .git metadata.",
                       "Values, tokens, cookies, auth headers and private keys are excluded."],
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"label": args.label, "path": str(output), "sha256": digest(output)}))


if __name__ == "__main__":
    main()
