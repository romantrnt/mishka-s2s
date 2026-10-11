"""Exercise a running hardware stand through HTTP and WebSocket transport.

This resets the selected stand. Run against an isolated test port unless an
operator has reserved the installation for testing. No microphone audio or
credentials are persisted by this script.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import struct
import time
from urllib.request import Request, urlopen


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--session", default="smoke")
    parser.add_argument("--token", default=os.getenv("STAND_ACCESS_TOKEN", ""))
    parser.add_argument("--skip-websocket", action="store_true")
    args = parser.parse_args()
    base = args.url.rstrip("/")
    headers = {"Content-Type": "application/json"}
    if args.token:
        headers["x-stand-token"] = args.token
    checks: list[dict[str, object]] = []

    def get(path: str) -> dict:
        with urlopen(Request(base + path, headers=headers), timeout=20) as response:
            return json.load(response)

    def post(path: str, payload: dict | None = None) -> dict:
        body = {"session_id": args.session, **(payload or {})}
        request = Request(base + path, data=json.dumps(body).encode(), headers=headers, method="POST")
        with urlopen(request, timeout=40) as response:
            return json.load(response)

    def check(name: str, condition: bool, detail: object = None) -> None:
        checks.append({"check": name, "ok": bool(condition), "detail": detail})

    post("/stand/reset")
    check("health", get("/health").get("status") == "ok")
    unknown = post("/stand/rfid", {"tag_id": "unregistered-tag"})
    check("unknown_rfid", unknown.get("state") != "AUTHENTICATED", unknown.get("reason"))
    post("/stand/reset")

    auth = post("/stand/rfid", {"tag_id": "test-child-001"})
    lifted = post("/stand/handset", {"state": "up"})
    check("child_rfid", auth.get("state") == "AUTHENTICATED", auth.get("state"))
    check("handset_up", lifted.get("node") == "fixture_welcome", lifted.get("node"))
    blocked = post("/stand/rfid", {"session_id": args.session + "-other", "tag_id": "test-child-001"})
    check("session_isolation", blocked.get("reason") == "installation_busy", blocked.get("reason"))

    for story_id in range(1, 6):
        post("/stand/reset")
        post("/stand/rfid", {"tag_id": "test-child-001"})
        post("/stand/handset", {"state": "up"})
        menu = post("/stand/button", {"button_id": 2})
        selected = post("/stand/button", {"button_id": story_id})
        state = get("/stand/state")
        story_commands = [item for item in state["commands"] if item["type"] == "audio_play" and item["payload"].get("content_kind") == "story"]
        check(f"story_command_{story_id}", menu.get("node") == "fixture_story_menu" and selected.get("node") == f"fixture_story_{story_id}" and bool(story_commands) and bool(story_commands[-1]["payload"].get("resolved_text")), selected.get("node"))
        post("/stand/button", {"button_id": 7})

    patterns = {1: [1], 2: [1, 3], 3: [1, 2, 3, 4, 5, 6, 7, 6, 5, 4, 3, 2, 1], 4: [2, 5, 1, 7]}
    for game_button, sequence in patterns.items():
        post("/stand/reset")
        post("/stand/rfid", {"tag_id": "test-child-001"})
        post("/stand/handset", {"state": "up"})
        post("/stand/button", {"button_id": 1})
        started = post("/stand/button", {"button_id": game_button})
        result = None
        for button_id in sequence:
            result = post("/stand/button", {"button_id": button_id})
        state = get("/stand/state")
        completed = [item for item in state["events"] if item.get("type") == "GameCompleted"]
        check(f"game_{game_button}", started.get("node", "").startswith("fixture_game_") and result is not None and result.get("node") == "fixture_choose" and bool(completed) and completed[-1]["payload"].get("success") is True, result.get("node") if result else None)

    post("/stand/reset")
    post("/stand/rfid", {"tag_id": "test-adult-dev-001"})
    dev = post("/stand/handset", {"state": "up"})
    before = get("/stand/state")
    ignored = post("/stand/button", {"button_id": 3})
    after = get("/stand/state")
    check("dev_button_no_cancel", dev.get("node") == "dev_dialogue" and ignored.get("node") == "dev_dialogue" and len([item for item in after["commands"] if item["type"] == "audio_stop"]) == len([item for item in before["commands"] if item["type"] == "audio_stop"]), ignored.get("reason"))
    text = post("/stand/text", {"text": "Привет"})
    text_state = get("/stand/state")
    audio_ready = any(item.get("event") == "audio_ready" for item in text_state["pipeline_metrics"].get("events", []))
    check("dev_text_audio", text.get("reason") in {"bounded_s2s", "quick_reply"} and audio_ready, {"reason": text.get("reason"), "audio_ready": audio_ready})
    post("/stand/handset", {"state": "down"})

    async def websocket_pcm() -> None:
        import websockets

        post("/stand/reset")
        post("/stand/rfid", {"tag_id": "test-adult-dev-001"})
        post("/stand/handset", {"state": "up"})
        address = base.replace("http://", "ws://").replace("https://", "wss://") + "/stand/ws"
        pcm = base64.b64encode(struct.pack("<" + "f" * 512, *([0.0] * 512))).decode()
        async with websockets.connect(address, additional_headers={"x-stand-token": args.token} if args.token else None) as websocket:
            first = json.loads(await asyncio.wait_for(websocket.recv(), 5))
            if first.get("type") == "admission_error":
                error = first.get("error", {})
                raise RuntimeError(
                    f"WebSocket admission rejected: {error.get('reason', 'unknown')} "
                    f"({error.get('code', 'unknown')})"
                )
            if first.get("type") != "stand_state":
                raise RuntimeError(f"unexpected WebSocket admission frame: {first}")
            began = time.perf_counter()
            for _ in range(100):
                await websocket.send(json.dumps({"type": "audio_pcm", "session_id": args.session, "sample_rate": 16000, "data_b64": pcm}))
            sent_ms = round((time.perf_counter() - began) * 1000, 1)
            for _ in range(40):
                await asyncio.sleep(0.05)
                state = get("/stand/state")
                count = sum(item.get("decoded", {}).get("type") == "audio_pcm" for item in state["raw_frames"])
                if count >= 100:
                    break
            else:
                count = 0
            try:
                extra = json.loads(await asyncio.wait_for(websocket.recv(), 0.3))
            except asyncio.TimeoutError:
                extra = None
            check("ws_pcm_ingress", first.get("type") == "stand_state" and count == 100, {"frames": count, "send_ms": sent_ms})
            check("ws_no_snapshot_flood", extra is None or extra.get("type") == "audio_pcm_output", extra.get("type") if isinstance(extra, dict) else None)
            check("pcm_redacted", all("data_b64" not in item.get("raw", "") for item in state["raw_frames"] if item.get("decoded", {}).get("type") == "audio_pcm"))

    if not args.skip_websocket:
        asyncio.run(websocket_pcm())
    post("/stand/reset")
    print(json.dumps({"passed": sum(item["ok"] for item in checks), "total": len(checks), "checks": checks}, ensure_ascii=False, indent=2))
    if not all(item["ok"] for item in checks):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
