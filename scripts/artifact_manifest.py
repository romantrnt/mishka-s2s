#!/usr/bin/env python3
"""Record and validate separated Mishka migration artifacts without copying caches."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = "mishka-artifact/v1"
ARTIFACT_TYPES = ("source_backup", "environment", "model_inventory", "ollama_cache")
FORBIDDEN_SOURCE = re.compile(r"^(?:\.venv|models|ollama-models|\.cache)(?:/|$)|^(?:\.env|ollama\.log)(?:$|\.)", re.I)
SECRET_VALUE = re.compile(r"(api[_-]?key|token|secret|password|credential)\s*[:=]", re.I)


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def metadata_digest(items: list[dict[str, Any]]) -> str:
    raw = json.dumps(items, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def env_names(root: Path) -> list[str]:
    names: set[str] = set()
    for candidate in (root / ".env.example", root / ".env.runpod"):
        if not candidate.is_file():
            continue
        for line in candidate.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key = line.split("=", 1)[0].strip()
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                names.add(key)
    return sorted(names)


def bootstrap_contract(root: Path) -> dict[str, str]:
    path = root / ".bootstrap-contract"
    result: dict[str, str] = {}
    if not path.is_file():
        return result
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            if key in {"IMAGE", "PYTHON", "TORCH", "CUDA", "NVIDIA_DRIVER", "WHISPER_MODEL", "LLM_MODEL"}:
                result[key] = value
    return result


def build_environment(root: Path) -> dict[str, Any]:
    os_info: dict[str, str] = {}
    release = Path("/etc/os-release")
    if release.is_file():
        for line in release.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                os_info[key] = value.strip('"')
    locks = {}
    for name in ("requirements-production.lock", "requirements-test.lock"):
        path = root / name
        if path.is_file():
            locks[name] = {"path": str(path), "sha256": sha256(path), "bytes": path.stat().st_size}
    return {
        "schema": SCHEMA,
        "artifact_type": "environment",
        "created_at": now(),
        "source": "read-only host/project inspection",
        "os": {"id": os_info.get("ID"), "version_id": os_info.get("VERSION_ID"), "codename": os_info.get("VERSION_CODENAME"), "pretty_name": os_info.get("PRETTY_NAME")},
        "base_image": bootstrap_contract(root).get("IMAGE"),
        "runtime": {"python": bootstrap_contract(root).get("PYTHON"), "torch": bootstrap_contract(root).get("TORCH"), "cuda": bootstrap_contract(root).get("CUDA"), "nvidia_driver": bootstrap_contract(root).get("NVIDIA_DRIVER"), "platform": platform.platform()},
        "lock_files": locks,
        "environment_names": env_names(root),
        "secret_values_included": False,
    }


def record_file(path: Path, root: Path) -> dict[str, Any]:
    return {"path": str(path.relative_to(root)), "exists": True, "bytes": path.stat().st_size, "sha256": sha256(path)}


def build_model_inventory(root: Path) -> dict[str, Any]:
    specs = [("whisper", "faster_whisper", root / "models/whisper"), ("rvc", "rvc", root / "models/rvc")]
    models: list[dict[str, Any]] = []
    for name, provider, path in specs:
        files = []
        if path.is_file():
            files = [record_file(path, root)]
        elif path.is_dir():
            files = [record_file(item, root) for item in sorted(path.rglob("*")) if item.is_file()]
        models.append({"model_name": name, "provider": provider, "version_or_revision": None, "path": str(path.relative_to(root)), "exists": path.exists(), "files": files, "bytes": sum(item["bytes"] for item in files), "content_digest": metadata_digest(files)})
    return {"schema": SCHEMA, "artifact_type": "model_inventory", "created_at": now(), "source": "read-only model path inspection", "models": models, "secret_values_included": False}

def build_ollama(root: Path, ollama_path: Path) -> dict[str, Any]:
    files = []
    if ollama_path.is_dir():
        for item in sorted(ollama_path.rglob("*")):
            if item.is_file() and "tmp" not in item.relative_to(ollama_path).parts:
                files.append({"path": str(item.relative_to(ollama_path)), "bytes": item.stat().st_size})
    models: list[str] = []
    binary = shutil.which("ollama")
    if binary:
        try:
            result = subprocess.run([binary, "list"], check=False, capture_output=True, text=True, timeout=10)
            for line in result.stdout.splitlines()[1:]:
                if line.strip():
                    models.append(line.split()[0])
        except (OSError, subprocess.SubprocessError):
            pass
    return {"schema": SCHEMA, "artifact_type": "ollama_cache", "created_at": now(), "source": "read-only Ollama/cache inspection", "cache_path": str(ollama_path), "exists": ollama_path.exists(), "files": files, "bytes": sum(item["bytes"] for item in files), "metadata_digest": metadata_digest(files), "models": models, "secret_values_included": False}


def build_source_backup(archive: Path) -> dict[str, Any]:
    members: list[str] = []
    forbidden: list[str] = []
    with tarfile.open(archive, "r:*") as stream:
        for member in stream.getmembers():
            name = member.name.removeprefix("./").lstrip("/")
            members.append(name)
            if FORBIDDEN_SOURCE.search(name):
                forbidden.append(name)
    return {"schema": SCHEMA, "artifact_type": "source_backup", "created_at": now(), "source": "existing archive validation", "archive": str(archive), "bytes": archive.stat().st_size, "sha256": sha256(archive), "member_count": len(members), "forbidden_members": sorted(forbidden), "source_only": not forbidden, "restore_command": f"tar -xzf {archive} -C .", "validation_command": f"sha256sum -c {archive}.sha256"}


def write_artifact(data: dict[str, Any], artifact_root: Path) -> Path:
    out_dir = artifact_root / data["artifact_type"]
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = data["created_at"].replace(":", "").replace("-", "")
    target = out_dir / f"{data['artifact_type']}-{stamp}.json"
    if target.exists():
        raise SystemExit(f"refusing to overwrite existing artifact: {target}")
    target.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    return target


def contains_secret_values(value: Any) -> bool:
    if isinstance(value, dict):
        return any(contains_secret_values(key) or contains_secret_values(item) for key, item in value.items())
    if isinstance(value, list):
        return any(contains_secret_values(item) for item in value)
    if isinstance(value, str):
        return bool(SECRET_VALUE.search(value))
    return False


def validate_manifest(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != SCHEMA:
        raise ValueError(f"{path}: invalid schema")
    kind = data.get("artifact_type")
    if kind not in ARTIFACT_TYPES:
        raise ValueError(f"{path}: invalid artifact_type")
    if not data.get("created_at") or data.get("secret_values_included", False):
        raise ValueError(f"{path}: missing timestamp or secret marker")
    if contains_secret_values(data):
        raise ValueError(f"{path}: probable secret value found")
    if kind == "source_backup" and data.get("forbidden_members"):
        raise ValueError(f"{path}: source archive contains forbidden cache/secret members")
    if kind == "environment" and not isinstance(data.get("environment_names"), list):
        raise ValueError(f"{path}: environment_names missing")
    if kind == "model_inventory" and not isinstance(data.get("models"), list):
        raise ValueError(f"{path}: models missing")
    if kind == "ollama_cache" and "cache_path" not in data:
        raise ValueError(f"{path}: cache_path missing")

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    record = sub.add_parser("record")
    record.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    record.add_argument("--artifact-root", type=Path, default=None)
    record.add_argument("--ollama-path", type=Path, default=None)
    record.add_argument("--source-archive", type=Path, default=None)
    validate = sub.add_parser("validate")
    validate.add_argument("path", type=Path)
    args = parser.parse_args()
    if args.command == "validate":
        paths = sorted(args.path.rglob("*.json")) if args.path.is_dir() else [args.path]
        if not paths:
            raise SystemExit(f"no JSON manifests found under {args.path}")
        for path in paths:
            validate_manifest(path)
        print(f"validated {len(paths)} artifact manifest(s)")
        return 0
    root = args.root.resolve()
    artifact_root = (args.artifact_root or root / "backups/artifacts").resolve()
    ollama_path = (args.ollama_path or Path(os.getenv("OLLAMA_MODELS", "models/ollama"))).resolve()
    written = [
        write_artifact(build_environment(root), artifact_root),
        write_artifact(build_model_inventory(root), artifact_root),
        write_artifact(build_ollama(root, ollama_path), artifact_root),
    ]
    if args.source_archive:
        archive = args.source_archive.resolve()
        if not archive.is_file():
            raise SystemExit(f"source archive not found: {archive}")
        source = build_source_backup(archive)
        if source["forbidden_members"]:
            raise SystemExit(f"refusing source archive with forbidden members: {source['forbidden_members'][:5]}")
        written.append(write_artifact(source, artifact_root))
    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
