"""Repeatable offline integration audit for the PTY/UART hardware stand."""

from __future__ import annotations

import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import asyncio
import random

from app.foundation.stand import HardwareIntegrationStand


async def send(stand, kind: str, payload: dict, session_id: str):
    result = await stand.inject(kind, payload, session_id)
    if isinstance(result, dict) and "error" in result:
        raise AssertionError((kind, payload, result))
    return result


async def start_child(stand, session_id: str):
    await send(stand, "rfid", {"tag_id": "mock-child-001"}, session_id)
    return await send(stand, "handset", {"state": "up"}, session_id)


async def check_games() -> None:
    games = {
        1: [1],
        2: [1, 3],
        3: list(range(1, 8)) + list(range(6, 0, -1)),
        4: [2, 5, 1, 7],
    }
    for choice, expected in games.items():
        stand = HardwareIntegrationStand()
        try:
            session_id = f"audit-game-{choice}"
            await start_child(stand, session_id)
            await asyncio.sleep(0.1)
            await send(stand, "button", {"button_id": 1}, session_id)
            result = await send(stand, "button", {"button_id": choice}, session_id)
            for button_id in expected:
                result = await send(stand, "button", {"button_id": button_id}, session_id)
            assert result["node"] == "fixture_choose", result
            assert any(
                event.get("type") == "GameCompleted"
                and event.get("payload", {}).get("success")
                for event in stand.snapshot()["events"]
            )
        finally:
            stand.close()


async def check_stories() -> None:
    for choice in range(1, 6):
        stand = HardwareIntegrationStand()
        try:
            session_id = f"audit-story-{choice}"
            await start_child(stand, session_id)
            await asyncio.sleep(0.1)
            await send(stand, "button", {"button_id": 2}, session_id)
            result = await send(stand, "button", {"button_id": choice}, session_id)
            assert result["node"] == f"fixture_story_{choice}", result
            result = await send(stand, "button", {"button_id": 7}, session_id)
            assert result["node"] == "fixture_choose", result
            assert any(
                command["type"] == "audio_play"
                and command["payload"].get("content_kind") == "story"
                for command in stand.snapshot()["commands"]
            )
        finally:
            stand.close()


async def check_dialogue_and_recovery() -> None:
    stand = HardwareIntegrationStand()
    try:
        await start_child(stand, "audit-fallback")
        await send(stand, "text", {"text": "Какая температура на Марсе?"}, "audit-fallback")
        assert stand.snapshot()["session"]["node"] == "fixture_fallback"
        result = await send(stand, "button", {"button_id": 1}, "audit-fallback")
        assert result["node"] == "fixture_choose", result
    finally:
        stand.close()

    stand = HardwareIntegrationStand()
    try:
        await send(stand, "rfid", {"tag_id": "mock-adult-dev-001"}, "audit-adult")
        result = await send(stand, "handset", {"state": "up"}, "audit-adult")
        assert result["node"] == "dev_dialogue", result
        result = await send(stand, "button", {"button_id": 3}, "audit-adult")
        assert result["reason"] != "safety_fallback", result
        result = await send(stand, "text", {"text": "Какая столица Японии?"}, "audit-adult")
        assert result["reason"] in {"bounded_s2s", "quick_reply"}, result
    finally:
        stand.close()


async def check_valid_event_fuzz() -> int:
    total = 0
    for seed in range(100):
        rng = random.Random(seed)
        stand = HardwareIntegrationStand()
        try:
            session_id = f"audit-fuzz-{seed}"
            for _ in range(100):
                choice = rng.randrange(7)
                if choice == 0:
                    kind, payload = "rfid", {"tag_id": rng.choice([
                        "mock-child-001", "mock-adult-dev-001", "unknown-tag"
                    ])}
                elif choice == 1:
                    kind, payload = "handset", {"state": rng.choice(["up", "down"])}
                elif choice in {2, 3, 4}:
                    kind, payload = "button", {"button_id": rng.randint(1, 7)}
                elif choice == 5:
                    kind, payload = "button_hold", {
                        "button_id": rng.randint(1, 7),
                        "duration_ms": rng.choice([100, 1000, 20000]),
                    }
                else:
                    kind, payload = "text", {"text": rng.choice([
                        "Привет", "Как настроение?", "Расскажи сказку",
                        "Какая столица Японии?", "Мне грустно",
                        "Какая температура на Марсе?",
                    ])}
                await send(stand, kind, payload, session_id)
                total += 1
        finally:
            stand.close()
    return total


async def main() -> None:
    await check_games()
    print("games: 4/4 successful paths")
    await check_stories()
    print("stories: 5/5 selection and completion paths")
    await check_dialogue_and_recovery()
    print("dialogue: child fallback/recovery and adult-dev path")
    total = await check_valid_event_fuzz()
    print(f"valid UART event fuzz: {total} events, 0 errors")


if __name__ == "__main__":
    asyncio.run(main())
