
#!/usr/bin/env python3
"""Canonical reproducible pytest runner for Mishka."""
from __future__ import annotations
import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shlex
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.foundation.acceptance_policy import POLICY_VERSION, default_claim, validate_report

SCHEMA = "mishka-regression/v1"
MODES = ("unit", "integration", "acceptance", "full", "smoke", "targeted")
ENVIRONMENT_MODES = ("non-production", "production")
MARKER_MODES = {"unit": "not integration and not acceptance and not hardware",
                "integration": "integration", "acceptance": "acceptance"}
SMOKE_TARGETS = ("-m", "smoke")

CONFIG_PREFIXES = (
    "MISHKA_", "STAND_", "NO_VOICE_", "AUDIO_", "RVC_", "OLLAMA_",
    "LLM_", "TTS_", "STT_", "VAD_", "VALIDATION_", "VLLM_",
    "YANDEX_", "FAST_AUDIO_", "INTENT_", "PLAYBACK_", "WHISPER_",
    "MAX_", "MIN_", "OBSERVABILITY_",
)
BASE_ENV_KEYS = {
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "TERM",
    "TMPDIR", "LD_LIBRARY_PATH", "CUDA_VISIBLE_DEVICES", "NVIDIA_VISIBLE_DEVICES",
}



def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def project():
    return Path(__file__).resolve().parents[1]


def test_python(root):
    configured = os.getenv("MISHKA_TEST_PYTHON")
    if configured:
        return Path(configured).expanduser().absolute()
    candidates = (
        root / ".test-venv/bin/python",
        root / ".test-venv-p1-20261007/bin/python",
        root / ".venv/bin/python",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return Path(sys.executable)


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_pins(path):
    pins = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^;#\s]+)", line)
        if not match:
            raise ValueError(f"{path}: unsupported lock line: {line}")
        pins[match.group(1).lower().replace("-", "_")] = match.group(2)
    return pins


def parse_env_file(path):
    values = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if value:
            try:
                tokens = shlex.split(value, comments=True, posix=True)
                value = tokens[0] if len(tokens) == 1 else " ".join(tokens)
            except ValueError as exc:
                raise ValueError(f"invalid environment file line for {key}") from exc
        values[key] = value
    return values


def _is_config_key(key):
    return key.startswith(CONFIG_PREFIXES) or key in {
        "STAND_ACCESS_TOKEN", "MISHKA_ALLOW_QUERY_TOKEN",
    }


def build_test_environment(root, mode):
    if mode not in ENVIRONMENT_MODES:
        raise ValueError(f"unsupported test environment: {mode}")
    env = {key: value for key, value in os.environ.items()
           if key in BASE_ENV_KEYS or not _is_config_key(key)}
    env.pop("MISHKA_TEST_ENVIRONMENT", None)
    env.pop("MISHKA_TEST_PYTHON", None)
    if mode == "production":
        env.update(parse_env_file(root / ".env.runpod"))
        env["MISHKA_TEST_ENVIRONMENT"] = "production"
    else:
        env["MISHKA_PROFILE"] = "mock"
        env["MISHKA_TEST_ENVIRONMENT"] = "non-production"
    return env


def capture(command, root, timeout=10, env=None):
    try:
        result = subprocess.run(command, cwd=root, text=True, capture_output=True,
                                timeout=timeout, check=False, env=env)
        return {"command": command, "returncode": result.returncode,
                "stdout": result.stdout[-4000:], "stderr": result.stderr[-4000:]}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"command": command, "returncode": None, "stdout": "",
                "stderr": f"{type(exc).__name__}: {exc}"}


def version_report(python, locked):
    result = {}
    for name, expected in locked.items():
        probe = subprocess.run(
            [str(python), "-c",
             f"import importlib.metadata as m; print(m.version({name!r}))"],
            text=True, capture_output=True, check=False,
        )
        actual = probe.stdout.strip() if probe.returncode == 0 else None
        result[name] = {"expected": expected, "actual": actual, "ok": actual == expected}
    return result


def environment(root, python, effective_env, mode):
    locks = {}
    for name in ("requirements-production.lock", "requirements-test.lock"):
        path = root / name
        if path.is_file():
            locks[name] = {"sha256": sha256(path), "bytes": path.stat().st_size}
    contract = {}
    path = root / ".bootstrap-contract"
    if path.is_file():
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in raw:
                key, value = raw.split("=", 1)
                if key in {"IMAGE", "PYTHON", "TORCH", "CUDA", "NVIDIA_DRIVER",
                           "WHISPER_MODEL", "LLM_MODEL"}:
                    contract[key] = value
    torch = capture([str(python), "-c",
                     "import torch; print(torch.__version__); print(torch.version.cuda)"],
                    root, 20, effective_env)
    listeners = capture(["ss", "-ltnp"], root, 5, effective_env)
    source = {"available": False, "reason": "git_metadata_unavailable"}
    if (root / ".git").exists():
        revision = capture(["git", "rev-parse", "HEAD"], root)
        status = capture(["git", "status", "--short"], root)
        source = {"available": revision["returncode"] == 0,
                  "revision": revision["stdout"].strip() or None,
                  "dirty": bool(status["stdout"].strip()) if status["returncode"] == 0 else None,
                  "status": status["stdout"].strip()[-2000:]}
    release = {}
    if Path("/etc/os-release").is_file():
        for raw in Path("/etc/os-release").read_text().splitlines():
            if "=" in raw:
                key, value = raw.split("=", 1)
                release[key] = value.strip('"')
    return {"platform": platform.platform(), "python": platform.python_version(),
            "python_executable": str(python),
            "test_environment_path": str(python.parent.parent),
            "shared_runtime_site_packages": os.getenv("MISHKA_TEST_SHARED_SITE_PACKAGES"),
            "os": {"id": release.get("ID"), "version_id": release.get("VERSION_ID"),
                   "pretty_name": release.get("PRETTY_NAME")},
            "bootstrap_contract": contract, "torch": torch, "locks": locks,
            "test_environment_mode": mode,
            "source": source, "runtime": {"canonical_port": 8000,
            "listeners": listeners, "runner_starts_runtime": False,
            "runner_restarts_runtime": False}}


def preflight(root, python, effective_env):
    lock = root / "requirements-test.lock"
    if not python.is_file():
        return {"ok": False, "reason": f"test Python is missing: {python}"}
    if not lock.is_file():
        return {"ok": False, "reason": "requirements-test.lock is missing"}
    locked = parse_pins(lock)
    versions = version_report(python, locked)
    pytest_probe = capture([str(python), "-c", "import pytest; print(pytest.__version__)"],
                           root, env=effective_env)
    imports = capture([str(python), "-c",
        "import app.foundation.api, app.foundation.s2s, app.foundation.runtime"],
        root, 30, effective_env)
    mismatches = [name for name, value in versions.items() if not value["ok"]]
    ok = pytest_probe["returncode"] == 0 and not mismatches and imports["returncode"] == 0
    return {"ok": ok, "pytest": {"installed": pytest_probe["returncode"] == 0,
            "version": pytest_probe["stdout"].strip() or None,
            "expected": locked.get("pytest"), "probe": pytest_probe},
            "test_lock": {"path": str(lock), "sha256": sha256(lock), "pins": locked},
            "dependency_versions": versions, "mismatches": mismatches,
            "production_imports": imports,
            "reason": None if ok else "test dependencies or production imports are not ready"}


def pytest_args(mode, targets, environment_mode="non-production"):
    if mode == "targeted":
        if not targets:
            raise ValueError("targeted mode requires at least one --target")
        return targets
    if mode == "smoke":
        return list(SMOKE_TARGETS)
    if mode in MARKER_MODES:
        return ["-m", MARKER_MODES[mode]]
    if mode == "full" and environment_mode == "non-production":
        return ["-m", "not acceptance"]
    return []


def failure_message(check, root, python):
    if check.get("pytest", {}).get("installed") is False:
        return ("pytest is missing. Install only the test overlay and rerun: "
                f"{python} -m pip install --requirement {root / 'requirements-test.lock'}")
    if check.get("mismatches"):
        return ("test dependency versions do not match requirements-test.lock: "
                + ", ".join(check["mismatches"]) + ". Repair with: "
                f"{python} -m pip install --requirement {root / 'requirements-test.lock'}")
    if check.get("production_imports", {}).get("returncode") != 0:
        return "production imports failed; inspect preflight.production_imports in the artifact"
    return check.get("reason", "regression preflight failed")


def store(path, text):
    path.write_text(text, encoding="utf-8")
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": sha256(path)}


def summary(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in reversed(lines):
        if re.search(r"\b(passed|failed|error|errors|skipped|warnings)\b", line, re.I):
            return line
    return lines[-1] if lines else None


def pytest_counts(text):
    """Extract pytest outcome counts without treating a summary as free text."""
    counts = {"passed": 0, "failed": 0, "skipped": 0, "errors": 0,
              "xfailed": 0, "xpassed": 0, "deselected": 0, "no_tests": False}
    for line in text.splitlines():
        if not re.search(r"\b(passed|failed|skipped|error|errors|xfailed|xpassed|deselected)\b",
                         line, re.I):
            continue
        for match in re.finditer(
                r"\b(\d+)\s+(passed|failed|skipped|error|errors|xfailed|xpassed|deselected)\b",
                line, re.I):
            value, name = int(match.group(1)), match.group(2).lower()
            counts["errors" if name in {"error", "errors"} else name] += value
    counts["no_tests"] = bool(re.search(r"no tests ran", text, re.I))
    return counts


def run(mode, targets, root, environment_mode):
    started = now()
    run_id = started.replace("-", "").replace(":", "").replace("+00:00", "Z")
    run_id += f"-{mode}-{uuid.uuid4().hex[:8]}"
    artifact = root / "acceptance/regression" / run_id
    artifact.mkdir(parents=True, exist_ok=False)
    python = test_python(root)
    effective_env = build_test_environment(root, environment_mode)
    env = environment(root, python, effective_env, environment_mode)
    check = preflight(root, python, effective_env)
    args = pytest_args(mode, targets, environment_mode)
    command = [str(python), "-m", "pytest", *args]
    stdout = stderr = ""
    status, code = "preflight_failed", 2
    if check["ok"]:
        try:
            child_env = {**effective_env, "PYTHONPATH": str(root)}
            result = subprocess.run(command, cwd=root,
                env=child_env, text=True,
                capture_output=True,
                timeout=float(os.getenv("MISHKA_REGRESSION_TIMEOUT_SECONDS", "1800")),
                check=False)
            stdout, stderr, code = result.stdout, result.stderr, result.returncode
            status = "passed" if code == 0 else "failed"
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = (exc.stderr or "") + "\nregression timeout expired\n"
            status, code = "timeout", 124
    else:
        message = failure_message(check, root, python)
        stdout = stderr = message + "\n"
    claim = default_claim(mode)
    report = {"schema": SCHEMA, "policy_version": POLICY_VERSION, "run_id": run_id,
              "mode": mode, "started_at": started, "finished_at": now(),
              "project_root": str(root), "source_snapshot": env["source"],
              "environment": {**claim["environment"], "metadata": env},
              "acceptance_type": claim["acceptance_type"],
              "real_dependencies": claim["real_dependencies"],
              "hardware_level": claim["hardware_level"],
              "test_doubles": claim["test_doubles"],
              "claim_ready": claim["claim_ready"],
              "readiness_evidence": claim["readiness_evidence"],
              "preflight": check, "command": command, "pytest_args": args,
              "test_environment_mode": environment_mode,
              "test_environment_path": env["test_environment_path"],
              "test_lock_sha256": env["locks"].get("requirements-test.lock", {}).get("sha256"),
              "test_counts": pytest_counts(stdout + "\n" + stderr),
              "status": status, "exit_code": code,
              "stdout": store(artifact / "stdout.log", stdout),
              "stderr": store(artifact / "stderr.log", stderr),
              "summary": summary(stdout + "\n" + stderr),
              "runtime_safety": {"runtime_started": False, "runtime_restarted": False,
                                 "canonical_port": 8000}}
    try:
        validate_report(report)
        report["policy_validation"] = {"valid": True, "errors": []}
    except ValueError as exc:
        report["policy_validation"] = {"valid": False, "errors": [str(exc)]}
        report["status"] = "policy_failed"
        report["exit_code"] = 2
        status, code = "policy_failed", 2
    (artifact / "report.json").write_text(json.dumps(report, indent=2,
        ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"status": status, "mode": mode, "exit_code": code,
                      "artifact": str(artifact / "report.json"),
                      "summary": report["summary"]}, ensure_ascii=False))
    if status == "preflight_failed":
        print(stdout, file=sys.stderr, end="")
    return code


def self_check(root):
    assert set(MODES) == {"unit", "integration", "acceptance", "full", "smoke", "targeted"}
    assert pytest_args("unit", []) == ["-m", MARKER_MODES["unit"]]
    assert pytest_args("smoke", []) == list(SMOKE_TARGETS)
    try:
        pytest_args("targeted", [])
    except ValueError:
        pass
    else:
        raise AssertionError("targeted mode accepted no target")
    assert parse_pins(root / "requirements-test.lock").get("pytest") == "9.1.1"
    print("regression harness self-check: PASS")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", nargs="?", choices=MODES, default="full")
    parser.add_argument("--target", action="append", default=[],
                        help="pytest path or node id; repeatable for targeted mode")
    parser.add_argument("--environment", choices=ENVIRONMENT_MODES,
                        default="non-production",
                        help="isolated test environment; production loads .env.runpod explicitly")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    root = project()
    return self_check(root) if args.self_check else run(
        args.mode, args.target, root, args.environment
    )


if __name__ == "__main__":
    raise SystemExit(main())

