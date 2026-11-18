#!/usr/bin/env python3
"""Pinned RVC100 worker for the historical VC API; preserves Applio baseline.

Private CLI and JSON protocol are compatible with RVCV2Provider. Conversion
success requires actual generator calls and valid retrieval queries, never a
passthrough. Model/asset hashes are immutable research candidate identities.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time

SOURCE_REVISION = "81eed5e8f68b6bed1789f682fe78cdd324495afc"
MODEL_SHA256 = "a08754de387e7307282a9eeeca7354d3c91c631e1bd293bea6a29bcfe9ec80bb"
INDEX_SHA256 = "06f1bf0e0b80d67da61b4ccdad7791aed1722e2ece4e83511a5108041f3f8b1b"
ASSET_HASHES = {
    "assets/hubert_base/config.json": "0346950779dfb7f9316fa74ed846e2b8a22a08eedfdc5387b73f327cb1a4a7cf",
    "assets/hubert_base/preprocessor_config.json": "7c1976a680fb7acc757cd36fb08eef878fa36c70b4c9d2d595df9c608bbbbf0e",
    "assets/hubert_base/pytorch_model.bin": "cc8c20f4b90a520757260197a3ff2505705a7adbd20ad9eeaa4e1a9b38442ef5",
    "assets/rmvpe/rmvpe.pt": "6d62215f4306e3ca278246188607209f09af3dc77ed4232efdd069798c4ec193",
}
SOURCE_HASHES = {
    "infer/cli.py": "8e321cd606bfa1f4756685db897e90df2278e77d3e58869bc83d4735ff7b7387",
    "infer/vc/modules.py": "7b2e12f56c2b2462d7944d8eda470796ffd4aec50f784c3f6f5169133eaae89b",
    "infer/vc/pipeline.py": "df8ccd98ca7f81e38934a0af93cc63b4f79764dc47d932f05914c566e876522a",
    "infer/hubert.py": "bbfba54d1d83eae3fb634b2909f8f6bf41e6ffe436200ec246867c5c93ebba26",
    "infer/rmvpe.py": "074171aec63e4b68f04d8691c62af50a1d99b9bc6dc56c4d6749fad4ba64853a",
    "configs/config.py": "f304463184b55898aa256fadeafd50d640ed0f880dbae6f89ad54db358240043",
}
MAX_COMMAND_BYTES = 16 * 1024
MAX_AUDIO_SECONDS = 60
NPROBE = 16


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def arguments(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--index", required=True)
    parser.add_argument("--f0-method", default="rmvpe")
    parser.add_argument("--filter-radius", type=float, default=3.0)
    parser.add_argument("--f0-up-key", type=int, default=0)
    parser.add_argument("--index-rate", type=float, default=.75)
    parser.add_argument("--protect", type=float, default=.33)
    args = parser.parse_args(argv)
    # RMVPE has no filter-radius argument in this pinned VC API; do not silently
    # pretend that a nonstandard filter setting changed the reproduced model.
    if (args.f0_method != "rmvpe" or args.filter_radius != 3.0
            or not math.isfinite(args.index_rate) or not 0 < args.index_rate <= 1
            or not math.isfinite(args.protect) or not 0 <= args.protect <= .5
            or not -24 <= args.f0_up_key <= 24):
        parser.error("unsupported trained RVC settings")
    return args


def validate_assets(root, model, index):
    for path, expected in [(model, MODEL_SHA256), (index, INDEX_SHA256),
                           *((root / p, digest) for p, digest in ASSET_HASHES.items()),
                           *((root / p, digest) for p, digest in SOURCE_HASHES.items())]:
        if not path.is_file() or sha(path) != expected:
            raise ValueError("trained RVC identity unavailable")
    # Source assets may be copied snapshots, so require exact critical source
    # hashes. When Git provenance exists, independently confirm its commit.
    if (root / ".git").exists():
        revision = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"],
                                           timeout=10, stderr=subprocess.DEVNULL).decode().strip()
        if revision != SOURCE_REVISION:
            raise ValueError("trained RVC source revision unavailable")


def counters():
    return dict(index_reads=0, index_queries=0, index_failures=0, full_probe_retries=0, model_calls=0)


class StrictIndex:
    """The recorded nprobe16/retry policy, with explicit swallowed-error proof."""

    def __init__(self, index, np, evidence):
        self.index, self.np, self.evidence = index, np, evidence
        if hasattr(index, "nprobe"):
            index.nprobe = min(NPROBE, index.nlist)

    def __getattr__(self, name):
        return getattr(self.index, name)

    def search(self, query, k):
        self.evidence["index_queries"] += 1
        try:
            scores, ids = self.index.search(query, k)
            if (ids < 0).any() and hasattr(self.index, "nprobe"):
                self.evidence["full_probe_retries"] += 1
                previous = self.index.nprobe
                self.index.nprobe = self.index.nlist
                try:
                    scores, ids = self.index.search(query, k)
                finally:
                    self.index.nprobe = previous
            if (ids < 0).any() or not self.np.isfinite(scores).all():
                raise RuntimeError("invalid retrieval neighbors")
            return self.np.maximum(scores, 1e-12), ids
        except Exception:
            self.evidence["index_failures"] += 1
            raise


def require_conversion_evidence(before, after):
    delta = {key: after[key] - before[key] for key in before}
    if delta["model_calls"] <= 0 or delta["index_queries"] <= 0 or delta["index_failures"] != 0:
        raise RuntimeError("trained RVC conversion not proved")
    return delta


def load_runtime(args, root, model, index):
    validate_assets(root, model, index)
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ.update(weight_root=str(model.parent), index_root=str(index.parent),
        outside_index_root=str(index.parent), rmvpe_root=str(root / "assets/rmvpe"),
        RVC_CUDA_GRAPH="0", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    import numpy as np
    import torch
    import transformers
    if transformers.__version__ != "4.49.0" or not torch.cuda.is_available():
        raise RuntimeError("trained RVC CUDA/runtime unavailable")
    import faiss
    import soundfile as sf
    from infer.cli import create_config
    from infer.vc.modules import VC
    from infer.vc.utils import load_hubert
    from infer.rmvpe import RMVPE
    evidence = counters()
    real_read = faiss.read_index
    # Load and reconstruct once: same exact vectors, kept resident. This removes
    # repeated disk reads from the existing per-block upstream pipeline call.
    raw_index = real_read(str(index))
    if raw_index.d != 768 or raw_index.ntotal <= 0:
        raise RuntimeError("trained RVC index incompatible")
    wrapped = StrictIndex(raw_index, np, evidence)
    vectors = raw_index.reconstruct_n(0, raw_index.ntotal)
    if not np.isfinite(vectors).all():
        raise RuntimeError("trained RVC index corrupt")
    # Upstream calls reconstruct_n for each conversion. Return the same verified
    # resident vector matrix without changing retrieval math.
    wrapped.reconstruct_n = lambda start, count: vectors if start == 0 and count == raw_index.ntotal else raw_index.reconstruct_n(start, count)
    def strict_read(path, *unused):
        evidence["index_reads"] += 1
        try:
            if Path(path).resolve() != index:
                raise RuntimeError("unexpected retrieval index")
            return wrapped
        except Exception:
            evidence["index_failures"] += 1
            raise
    faiss.read_index = strict_read
    config = create_config()
    if not str(config.device).startswith("cuda"):
        raise RuntimeError("trained RVC device unavailable")
    vc = VC(config)
    vc.get_vc(model.name)
    if vc.version != "v2" or vc.if_f0 != 1 or vc.tgt_sr != 40000:
        raise RuntimeError("trained RVC model incompatible")
    vc.hubert_model = load_hubert(config)
    vc.pipeline.model_rmvpe = RMVPE(str(root / "assets/rmvpe/rmvpe.pt"),
                                  is_half=config.is_half, device=config.device)
    original_infer = vc.net_g.infer
    def recorded(*pos, **kw):
        evidence["model_calls"] += 1
        return original_infer(*pos, **kw)
    vc.net_g.infer = recorded
    torch.cuda.synchronize()
    return vc, evidence, np, sf, torch, transformers, raw_index


def emit(protocol, value):
    line = json.dumps(value, separators=(",", ":"), allow_nan=False) + "\n"
    if len(line.encode()) > 8192:
        raise ValueError("reply oversized")
    protocol.write(line)
    protocol.flush()


def main():
    args = arguments()
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", buffering=1)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    try:
        root = Path(args.project_root).resolve(strict=True)
        model = Path(args.model).resolve(strict=True)
        index = Path(args.index).resolve(strict=True)
        vc, evidence, np, sf, torch, transformers, raw_index = load_runtime(args, root, model, index)
        identity = dict(actual_route="rvc100", model_sha256=MODEL_SHA256, index_sha256=INDEX_SHA256,
            source_revision=SOURCE_REVISION, runtime_profile="rvc-recovery-freeze", native_streaming=False,
            retrieval_nprobe=NPROBE, sample_rate=40000, fallback_used=False, passthrough_used=False)
        emit(protocol, {"event": "ready", "pid": os.getpid(), "device": str(vc.config.device),
            "model_version": "v2", "index_vectors": int(raw_index.ntotal), "index_dimension": int(raw_index.d),
            "torch": str(torch.__version__), "transformers": transformers.__version__, **identity})
        while True:
            line = sys.stdin.buffer.readline(MAX_COMMAND_BYTES + 1)
            if not line:
                return 0
            request_id = None
            try:
                if len(line) > MAX_COMMAND_BYTES or not line.endswith(b"\n"):
                    raise ValueError("command oversized")
                request = json.loads(line)
                request_id = request.get("id")
                if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", request_id):
                    raise ValueError("invalid request ID")
                if request.get("op") == "shutdown":
                    emit(protocol, {"event": "stopped", "id": request_id})
                    return 0
                if request.get("op") == "ping":
                    emit(protocol, {"event": "pong", "id": request_id, "route_evidence": dict(evidence), **identity})
                    continue
                if request.get("op") != "convert":
                    raise ValueError("unsupported operation")
                source = Path(request["input"]).resolve(strict=True)
                output = Path(request["output"]).resolve(strict=False)
                if source == output or source.parent != output.parent or not output.parent.is_dir():
                    raise ValueError("invalid PCM paths")
                info = sf.info(str(source))
                if info.channels != 1 or not 0 < info.duration <= MAX_AUDIO_SECONDS:
                    raise ValueError("invalid input PCM")
                before = dict(evidence)
                torch.manual_seed(42)
                np.random.seed(42)
                torch.cuda.synchronize()
                started = time.perf_counter()
                _, result = vc.vc_single(0, str(source), args.f0_up_key, "rmvpe", str(index),
                    args.index_rate, 0, 1.0, args.protect)
                torch.cuda.synchronize()
                elapsed = (time.perf_counter() - started) * 1000
                if not result or result[0] != 40000 or result[1] is None:
                    raise RuntimeError("trained RVC inference failed")
                pcm = np.asarray(result[1])
                if (pcm.ndim != 1 or not 0 < len(pcm) <= 40000 * MAX_AUDIO_SECONDS
                        or pcm.dtype != np.int16 or not np.isfinite(pcm).all() or not np.any(pcm)):
                    raise RuntimeError("invalid trained RVC PCM")
                delta = require_conversion_evidence(before, evidence)
                sf.write(str(output), pcm, 40000, format="WAV", subtype="PCM_16")
                emit(protocol, {"event": "converted", "id": request_id, "elapsed_ms": elapsed,
                    "output_bytes": output.stat().st_size, "route_evidence": delta, **identity})
            except Exception:
                emit(protocol, {"event": "error", "id": request_id, "error": "trained_rvc_conversion_failed"})
                # Parent kills a failed worker. Do not consume another command
                # after a malformed/truncated line or a swallowed vendor error.
                return 1
    except Exception:
        emit(protocol, {"event": "error", "error": "trained_rvc_start_failed"})
        return 1
    finally:
        protocol.close()


if __name__ == "__main__":
    raise SystemExit(main())
