#!/usr/bin/env python3
"""Reproducible process-resource benchmark for one Mishka runtime."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

def process_for_port(port):
    try:
        text = subprocess.check_output(["ss", "-ltnp"], text=True, stderr=subprocess.DEVNULL)
        for line in text.splitlines():
            if f":{port} " not in line and not line.rstrip().endswith(f":{port}"):
                continue
            match = re.search(r"pid=(\d+)", line)
            if match:
                return int(match.group(1))
    except (OSError, subprocess.CalledProcessError):
        pass
    try:
        text = subprocess.check_output(["ps", "-eo", "pid=,args="], text=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    for line in text.splitlines():
        match = re.match(r"\s*(\d+)\s+(.*)", line)
        if match and f"--port {port}" in match.group(2) and ("python" in match.group(2) or "uvicorn" in match.group(2)):
            return int(match.group(1))
    return None

def proc_snapshot(pid):
    values = {"rss_kb": None, "hwm_kb": None, "vm_size_kb": None, "pss_kb": None, "threads": None}
    if not pid:
        return values
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            key, _, value = line.partition(":")
            number = value.strip().split()[0] if value.strip() else ""
            if key == "VmRSS": values["rss_kb"] = int(number)
            elif key == "VmHWM": values["hwm_kb"] = int(number)
            elif key == "VmSize": values["vm_size_kb"] = int(number)
            elif key == "Threads": values["threads"] = int(number)
    except (FileNotFoundError, PermissionError, ValueError):
        return values
    try:
        for line in Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines():
            if line.startswith("Pss:"):
                values["pss_kb"] = int(line.split()[1])
                break
    except (FileNotFoundError, PermissionError, ValueError):
        pass
    return values

def process_meta(pid):
    if not pid:
        return {"pid": None, "cmdline": None, "start_time": None}
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
        stat = Path(f"/proc/{pid}/stat").read_text().split()
        return {"pid": pid, "cmdline": cmdline[:200], "start_time": stat[21] if len(stat) > 21 else None}
    except (FileNotFoundError, PermissionError, IndexError):
        return {"pid": pid, "cmdline": None, "start_time": None}

def request(base, path, *, token=None, payload=None, timeout=30.0):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["X-Stand-Token"] = token
    req = urllib.request.Request(base + path, data=data, headers=headers, method="POST" if data is not None else "GET")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read()
            return {"path": path, "status": response.status, "elapsed_s": round(time.monotonic() - started, 3), "body_bytes": len(body)}
    except urllib.error.HTTPError as exc:
        body = exc.read()
        return {"path": path, "status": exc.code, "elapsed_s": round(time.monotonic() - started, 3), "body_bytes": len(body), "error": "http_error"}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"path": path, "status": None, "elapsed_s": round(time.monotonic() - started, 3), "body_bytes": None, "error": type(exc).__name__}



def source_fingerprint():
    files = [ROOT / "app/foundation/api.py", ROOT / "app/foundation/config.py", ROOT / "app/foundation/schedule_weather.py", ROOT / "scripts/resource_benchmark.py"]
    return {"git_available": (ROOT / ".git").exists(), "files_sha256": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}}

def canonical_iteration(base, token, timeout, session):
    calls = [
        request(base, "/stand/reset", token=token, payload={"session_id": session}, timeout=timeout),
        request(base, "/stand/rfid", token=token, payload={"session_id": session, "tag_id": "test-child-001"}, timeout=timeout),
        request(base, "/stand/handset", token=token, payload={"session_id": session, "state": "up"}, timeout=timeout),
        request(base, "/stand/button", token=token, payload={"session_id": session, "button_id": 7}, timeout=timeout),
        request(base, "/stand/handset", token=token, payload={"session_id": session, "state": "down"}, timeout=timeout),
        request(base, "/stand/reset", token=token, payload={"session_id": session}, timeout=timeout),
    ]
    return {"session": session, "calls": calls, "ok": all(call["status"] in {200, 202} for call in calls)}

def run(port, label, repeats, warmups, timeout):
    base = f"http://127.0.0.1:{port}"
    pid = process_for_port(port)
    token = os.environ.get("STAND_ACCESS_TOKEN", "") if port == 8000 else ""
    report = {
        "schema": "mishka/resource-benchmark/v1", "started_at": utc_now(), "label": label, "port": port,
        "pid": process_meta(pid), "process": proc_snapshot(pid),
        "environment": {"profile": os.environ.get("MISHKA_PROFILE", "unknown"), "voice_mode": os.environ.get("MISHKA_VOICE_CONVERSION", "unknown"), "python": platform.python_version(), "platform": platform.platform()},
        "provenance": source_fingerprint(), "request_timeout_s": timeout,
        "policy": {"legacy_mutation": "forbidden" if port == 8015 else "canonical simulator/API boundary", "target_budget": "not supplied; baseline only"},
        "stages": [], "failures": [],
    }
    if not pid:
        report["failures"].append("runtime_process_not_found")
        return report, 2
    first = proc_snapshot(pid)
    health = request(base, "/health", token=token or None, timeout=timeout)
    ready = request(base, "/ready", token=token or None, timeout=timeout)
    report["stages"].append({"name": "readiness_probe", "calls": [health, ready], "memory": proc_snapshot(pid)})
    if health["status"] != 200 or ready["status"] not in {200, 503}:
        report["failures"].append("readiness_probe_transport")
    if port == 8000 and not token:
        report["failures"].append("canonical_token_missing_from_environment")
    for index in range(max(0, warmups)):
        if port == 8000 and token:
            canonical_iteration(base, token, timeout, f"wp05-bench-warmup-{index}")
        else:
            request(base, "/health", timeout=timeout)
    iterations = []
    for index in range(max(1, repeats)):
        before = proc_snapshot(pid)
        if port == 8000 and token:
            outcome = canonical_iteration(base, token, timeout, f"wp05-bench-{index}")
        else:
            calls = [request(base, "/health", timeout=timeout), request(base, "/ready", timeout=timeout)]
            outcome = {"read_only": True, "calls": calls, "ok": all(call["status"] in {200, 503} for call in calls)}
        after = proc_snapshot(pid)
        iterations.append({"index": index, "outcome": outcome, "memory_before": before, "memory_after": after})
        if not outcome.get("ok"):
            report["failures"].append(f"iteration_{index}_failed")
    final_health = request(base, "/health", token=token or None, timeout=timeout)
    final_ready = request(base, "/ready", token=token or None, timeout=timeout)
    report["stages"].append({"name": "iterations", "items": iterations})
    report["stages"].append({"name": "post_cleanup_probe", "calls": [final_health, final_ready], "memory": proc_snapshot(pid)})
    report["memory"] = {"before": first, "after": proc_snapshot(pid), "iterations": [{"before": item["memory_before"], "after": item["memory_after"]} for item in iterations]}
    report["finished_at"] = utc_now()
    report["result"] = "PASS" if not report["failures"] else "FAIL"
    return report, 0 if not report["failures"] else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True, choices=(8000, 8015))
    parser.add_argument("--label", required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--request-timeout", type=float, default=30.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report, code = run(args.port, args.label, max(1, args.repeats), max(0, args.warmups), args.request_timeout)
    output = args.output or ROOT / "acceptance/benchmarks" / f"wp05-{args.label}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"label": args.label, "port": args.port, "pid": report["pid"].get("pid"), "result": report["result"], "failures": report["failures"], "output": str(output)}, ensure_ascii=False, sort_keys=True))
    return code

if __name__ == "__main__":
    raise SystemExit(main())
