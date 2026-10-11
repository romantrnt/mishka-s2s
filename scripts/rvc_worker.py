#!/usr/bin/env python3
"""Persistent, single-owner Applio RVC inference worker.

The parent process communicates over newline-delimited JSON on stdin/stdout.
All third-party output is redirected to stderr so it cannot corrupt the
protocol. Audio remains in a RAM-backed directory owned by the parent.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
import traceback
from pathlib import Path



def _emit(stream, payload: dict) -> None:
    stream.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    stream.flush()


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--index", required=True)
    parser.add_argument("--f0-method", default="rmvpe")
    parser.add_argument("--filter-radius", type=float, default=3.0)
    parser.add_argument("--f0-up-key", type=int, default=0)
    parser.add_argument("--index-rate", type=float, default=0.0)
    parser.add_argument("--protect", type=float, default=0.33)
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    project_root = Path(args.project_root).resolve()
    model_path = Path(args.model).resolve()
    index_path = Path(args.index).resolve()
    if not project_root.is_dir() or not model_path.is_file() or not index_path.is_file():
        raise RuntimeError("RVC project/model/index is missing")

    # Keep a private protocol stream, then send every library print (including
    # native-extension diagnostics) to stderr.
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", buffering=1)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    os.chdir(project_root)
    sys.path.insert(0, str(project_root))
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

    with contextlib.redirect_stdout(sys.stderr):
        # ContentVec is audio-only. The base RunPod image carries a
        # torchvision build for a different torch ABI; prevent Transformers'
        # optional vision branch from importing it into this audio worker.
        import transformers.utils.import_utils as _transformers_imports
        import transformers.utils as _transformers_utils
        _transformers_imports.is_torchvision_available = lambda: False
        _transformers_utils.is_torchvision_available = _transformers_imports.is_torchvision_available
        import faiss
        import torch
        from rvc.infer.infer import VoiceConverter

        converter = VoiceConverter()
        converter.get_vc(str(model_path), 0)
        converter.load_hubert("contentvec")
        converter.last_embedder_model = "contentvec"

        # Fail at startup if the retrieval index is unreadable or incompatible.
        index = faiss.read_index(str(index_path))
        index_vectors = int(index.ntotal)
        index_dimension = int(index.d)
        del index

    _emit(
        protocol,
        {
            "event": "ready",
            "pid": os.getpid(),
            "device": str(converter.config.device),
            "sample_rate": int(converter.tgt_sr),
            "model_version": str(converter.version),
            "vocoder": str(converter.vocoder),
            "index_vectors": index_vectors,
            "index_dimension": index_dimension,
            "torch": str(torch.__version__),
            "cuda": str(torch.version.cuda),
        },
    )

    for raw in sys.stdin:
        request_id = None
        try:
            request = json.loads(raw)
            request_id = str(request.get("id", ""))
            operation = request.get("op")
            if operation == "shutdown":
                _emit(protocol, {"event": "stopped", "id": request_id})
                return 0
            if operation == "ping":
                _emit(protocol, {"event": "pong", "id": request_id})
                continue
            if operation != "convert" or not request_id:
                raise ValueError("invalid worker request")

            input_path = Path(request["input"]).resolve()
            output_path = Path(request["output"]).resolve()
            if not input_path.is_file():
                raise FileNotFoundError("RVC input is missing")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            started = time.perf_counter()
            with contextlib.redirect_stdout(sys.stderr):
                converter.convert_audio(
                    audio_input_path=str(input_path),
                    audio_output_path=str(output_path),
                    model_path=str(model_path),
                    index_path=str(index_path),
                    pitch=args.f0_up_key,
                    f0_method=args.f0_method,
                    filter_radius=args.filter_radius,
                    index_rate=args.index_rate,
                    volume_envelope=1.0,
                    protect=args.protect,
                    split_audio=False,
                    f0_autotune=False,
                    embedder_model="contentvec",
                    clean_audio=False,
                    export_format="WAV",
                    post_process=False,
                )
            if not output_path.is_file() or output_path.stat().st_size <= 44:
                raise RuntimeError("RVC produced no audio")
            _emit(
                protocol,
                {
                    "event": "converted",
                    "id": request_id,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                    "output_bytes": output_path.stat().st_size,
                },
            )
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            _emit(
                protocol,
                {
                    "event": "error",
                    "id": request_id,
                    "error": type(exc).__name__,
                    "message": str(exc)[:500],
                },
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
