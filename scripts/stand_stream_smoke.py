"""Measure real text→LLM→Silero→RVC→browser PCM and barge-in on a test stand.

The test resets the selected stand and must not be pointed at an installation
currently used by another tester. It never writes conversation audio to disk.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import os
import struct
import time
from urllib.request import Request, urlopen

import websockets
from websockets.exceptions import ConnectionClosed


def websocket_address(base: str, token: str = "") -> str:
    if base.startswith("https://"):
        address = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        address = "ws://" + base[len("http://"):]
    else:
        raise ValueError("--url must start with http:// or https://")
    address += "/stand/ws"
    return address


def decode_output_pcm(message: dict) -> tuple[tuple[int, ...], int, int]:
    if message.get("type") != "audio_pcm_output":
        raise ValueError("not an audio_pcm_output frame")
    try:
        sample_rate = int(message["sample_rate"])
        token = int(message["token"])
        raw = base64.b64decode(str(message["data_b64"]), validate=True)
    except (KeyError, TypeError, ValueError, base64.binascii.Error) as exc:
        raise ValueError("malformed audio PCM output") from exc
    if sample_rate <= 0 or not raw or len(raw) % 2:
        raise ValueError("audio output must be non-empty little-endian PCM16")
    samples = struct.unpack("<" + "h" * (len(raw) // 2), raw)
    if not all(math.isfinite(value) and -32768 <= value <= 32767 for value in samples):
        raise ValueError("audio output contains invalid PCM samples")
    return samples, sample_rate, token


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--token", default=os.getenv("STAND_ACCESS_TOKEN", ""))
    parser.add_argument("--session", default="stream-smoke")
    parser.add_argument("--text", default="Привет")
    parser.add_argument("--timeout", type=float, default=25.0)
    args = parser.parse_args()
    base = args.url.rstrip("/")
    headers = {"Content-Type": "application/json"}
    if args.token:
        headers["x-stand-token"] = args.token

    def post(path: str, data: dict) -> dict:
        body = json.dumps({"session_id": args.session, **data}).encode()
        request = Request(base + path, body, headers, method="POST")
        with urlopen(request, timeout=40) as response:
            return json.load(response)

    def state() -> dict:
        with urlopen(Request(base + "/stand/state", headers=headers), timeout=15) as response:
            return json.load(response)

    await asyncio.to_thread(post, "/stand/reset", {})
    await asyncio.to_thread(post, "/stand/rfid", {"tag_id": "test-adult-dev-001"})
    await asyncio.to_thread(post, "/stand/handset", {"state": "up"})
    address = websocket_address(base, args.token)
    report: dict[str, object] = {}
    async with websockets.connect(
        address,
        additional_headers={"x-stand-token": args.token} if args.token else None,
        max_size=16 * 1024 * 1024,
        open_timeout=10,
        close_timeout=5,
    ) as websocket:
        first = json.loads(await asyncio.wait_for(websocket.recv(), 5))
        if first.get("type") == "admission_error":
            error = first.get("error", {})
            raise RuntimeError(
                f"WebSocket admission rejected: {error.get('reason', 'unknown')} "
                f"({error.get('code', 'unknown')})"
            )
        if first.get("type") != "stand_state":
            raise RuntimeError(f"unexpected WebSocket admission frame: {first}")
        started = time.perf_counter()
        await websocket.send(json.dumps({"type": "text", "session_id": args.session, "text": args.text}, ensure_ascii=False))
        first_task = asyncio.create_task(asyncio.to_thread(state))
        first_token: int | None = None
        first_deadline = time.perf_counter() + args.timeout
        while time.perf_counter() < first_deadline:
            message = json.loads(await asyncio.wait_for(websocket.recv(), min(10.0, max(0.1, first_deadline - time.perf_counter()))))
            if message.get("type") == "audio_pcm_output":
                _, sample_rate, output_token = decode_output_pcm(message)
                if sample_rate <= 0:
                    raise RuntimeError("invalid output sample rate")
                first_token = output_token
                report["first_audio_ms"] = round((time.perf_counter() - started) * 1000, 1)
                break
        if first_token is None:
            raise RuntimeError("no real PCM output from WebSocket text input")
        pcm_before = len((await asyncio.to_thread(state)).get("raw_frames", []))
        pcm = base64.b64encode(struct.pack("<" + "f" * 512, *([0.2] * 512))).decode()
        silence = base64.b64encode(struct.pack("<" + "f" * 320, *([0.0] * 320))).decode()
        for _ in range(12):
            await websocket.send(json.dumps({"type": "audio_pcm", "session_id": args.session, "sample_rate": 16000, "data_b64": pcm}))
        for _ in range(14):
            await websocket.send(json.dumps({"type": "audio_pcm", "session_id": args.session, "sample_rate": 16000, "data_b64": silence}))
        await websocket.send(json.dumps({"type": "flush_audio", "session_id": args.session}))
        pcm_input_seen = False
        pcm_audio_frames = 0
        deadline = time.perf_counter() + args.timeout
        while time.perf_counter() < deadline:
            snapshot = await asyncio.to_thread(state)
            pcm_input_seen = len(snapshot.get("raw_frames", [])) > pcm_before
            try:
                message = json.loads(await asyncio.wait_for(websocket.recv(), 0.5))
            except asyncio.TimeoutError:
                if pcm_input_seen:
                    break
                continue
            if message.get("type") == "error":
                raise RuntimeError(f"WebSocket protocol error: {message}")
            if message.get("type") == "audio_pcm_output":
                decode_output_pcm(message)
                pcm_audio_frames += 1
            if pcm_input_seen and pcm_audio_frames:
                break
        first_result = await first_task
    snapshot = await asyncio.to_thread(state)
    events = snapshot.get("pipeline_metrics", {}).get("events", [])
    report.update({
        "first_reason": first_result.get("reason"),
        "first_node": first_result.get("node"),
        "first_fallback": [item.get("reason") for item in first_result.get("outputs", []) if item.get("action") == "s2s_fallback"],
        "first_token": first_token,
        "pcm_input_frames_seen": len(snapshot.get("raw_frames", [])) > pcm_before,
        "pcm_audio_frames": pcm_audio_frames,
        "token_events": sum(item.get("event") == "token_received" for item in events),
        "chunk_events": sum(item.get("event") == "chunk_closed" for item in events),
        "tts_events": sum(item.get("event") == "tts_started" for item in events),
        "audio_ready_events": sum(item.get("event") == "audio_ready" for item in events),
    })
    await asyncio.to_thread(post, "/stand/reset", {})
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report.get("pcm_input_frames_seen") or not report.get("first_token"):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
