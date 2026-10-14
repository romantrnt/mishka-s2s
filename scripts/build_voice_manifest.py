#!/usr/bin/env python3
"""Build the canonical manifest; discovery never implies content approval."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import struct
import wave
from pathlib import Path

REQUIRED_COUNT = 108
PLACEHOLDER_IDS = frozenset()
ALLOWED_SAMPLE_RATES = frozenset({22050, 24000, 48000})
MAX_CLIPPED_RATIO = 0.01


def inspect_wav(path: Path, *, allow_silence: bool = False) -> dict:
    """Read the complete WAV deterministically; never infer approval."""
    result = {
        "format": "wav_pcm_s16le", "sample_rate": None, "channels": None,
        "sample_width_bits": None, "frame_count": None, "duration_ms": None,
        "rms": None, "peak_abs": None, "clipped_sample_count": None,
        "validation_status": "missing", "validation_reason": "file_missing",
    }
    if not path.is_file():
        return result
    try:
        with wave.open(str(path), "rb") as source:
            channels = source.getnchannels(); width = source.getsampwidth()
            rate = source.getframerate(); frames = source.getnframes()
            compression = source.getcomptype(); raw = source.readframes(frames)
        result.update({"sample_rate": rate, "channels": channels,
                       "sample_width_bits": width * 8, "frame_count": frames,
                       "duration_ms": round(frames * 1000 / rate) if rate else None})
        expected = frames * channels * width
        if compression != "NONE":
            raise ValueError("compressed_wav")
        if channels != 1:
            raise ValueError("channels_must_be_mono")
        if width != 2:
            raise ValueError("sample_width_must_be_16_bit")
        if rate not in ALLOWED_SAMPLE_RATES:
            raise ValueError("unsupported_sample_rate")
        if frames <= 0 or not raw or len(raw) != expected:
            raise ValueError("empty_or_truncated_payload")
        samples = struct.unpack("<%dh" % frames, raw)
        peak = max(abs(value) for value in samples)
        square_mean = sum(value * value for value in samples) / frames
        clipped = sum(1 for value in samples if abs(value) >= 32767)
        result.update({"rms": round(math.sqrt(square_mean), 2), "peak_abs": peak,
                       "clipped_sample_count": clipped})
        if not allow_silence and peak == 0:
            raise ValueError("silent_audio")
        if clipped / frames > MAX_CLIPPED_RATIO:
            raise ValueError("excessive_clipping")
        result.update({"validation_status": "valid", "validation_reason": None})
    except (OSError, EOFError, struct.error, wave.Error, ValueError) as exc:
        result["validation_status"] = "invalid"
        result["validation_reason"] = str(exc)[:80]
    return result


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", type=Path, default=Path("content/audio/phrase_catalog_ru.txt"))
    parser.add_argument("--manifest", type=Path, default=Path("content/audio/voice_manifest.json"))
    parser.add_argument("--approve", action="store_true")
    parser.add_argument("--approved-by")
    args = parser.parse_args()
    phrases = [line.strip() for line in args.catalog.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(phrases) != REQUIRED_COUNT:
        raise ValueError(f"catalog must contain {REQUIRED_COUNT} phrases")
    if args.approve and not args.approved_by:
        raise ValueError("--approved-by is required with --approve")
    root = args.manifest.parent
    assets = []
    for number, transcript in enumerate(phrases, 1):
        phrase_id = f"PHRASE_{number:03d}"
        asset_path = f"voice/{phrase_id}.wav"
        recording = root / asset_path
        recorded = recording.is_file()
        audio = inspect_wav(recording, allow_silence=phrase_id in PLACEHOLDER_IDS)
        status = "missing" if not recorded else "invalid" if audio["validation_status"] != "valid" else (
            "placeholder" if phrase_id in PLACEHOLDER_IDS else "approved" if args.approve else "recorded_pending_approval")
        assets.append({
            "phrase_id": phrase_id, "transcript": transcript, "language": "ru-RU",
            "voice_version": "mishka-v1", "content_hash": digest(transcript.encode("utf-8")),
            "asset_path": asset_path, "asset_sha256": digest(recording.read_bytes()) if recorded else None,
            "version": 1, "status": status,
            "development_only": phrase_id in PLACEHOLDER_IDS,
            "placeholder_kind": "silence_no_speech" if phrase_id in PLACEHOLDER_IDS else None,
            "approved_by": args.approved_by if status == "approved" else None,
            "approved_at": None,
            **audio,
        })
    approved = sum(item["status"] == "approved" for item in assets)
    placeholders = sum(item["status"] == "placeholder" for item in assets)
    document = {
        "contract": "mishka.voice-manifest/v1",
        "status": "ready" if approved == REQUIRED_COUNT else "incomplete_pending_recording_or_approval",
        "required_recorded_count": REQUIRED_COUNT, "required_approved_count": REQUIRED_COUNT,
        "source_catalog": args.catalog.name,
        "audio_contract": {"format": "wav_pcm_s16le", "sample_rates_hz": sorted(ALLOWED_SAMPLE_RATES),
                           "channels": 1, "sample_width_bits": 16, "silence_policy": "placeholder_only"},
        "recorded_count": sum(item["asset_sha256"] is not None for item in assets),
        "missing_count": sum(item["status"] == "missing" for item in assets),
        "invalid_count": sum(item["status"] == "invalid" for item in assets),
        "placeholder_count": placeholders, "approved_count": approved,
        "validation": {"valid_count": sum(item["validation_status"] == "valid" for item in assets),
                       "invalid_count": sum(item["validation_status"] != "valid" for item in assets)},
        "assets": assets,
    }
    args.manifest.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: document[key] for key in ("status", "recorded_count", "approved_count", "required_recorded_count")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
