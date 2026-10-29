#!/usr/bin/env python3
"""Deterministic clean-process smoke for the canonical exhibition runtime."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.foundation.config import Settings
from app.foundation.lighting import LIGHTING_ELEMENT_IDS
from app.foundation.stand import HardwareIntegrationStand


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


async def scenario() -> dict[str, object]:
    clock = [100.0]
    settings = Settings(profile="mock", runtime_mode="exhibition", hardware_mode="simulator", content_root="content").validate()
    stand = HardwareIntegrationStand(settings=settings)
    try:
        stand.application.monotonic_now = lambda: clock[0]
        stand.application.canonical_runtime.monotonic_now = stand.application.monotonic_now
        stand.application.led.exhibition_effects.monotonic_now = stand.application.monotonic_now

        health = stand.health_snapshot()
        check(health["mode"] == "exhibition", "startup mode is not exhibition")
        check(health["canonical_state"] == "idle", "startup state is not idle")
        check(health["handset"] == "down", "startup handset is not down")
        check(health["status"] in {"ready", "degraded"}, "health is unavailable")
        check(stand.input_adapter is None, "simulator created a physical HID reader")
        check(stand.application.canonical_runtime.sessions == {}, "startup created a session")
        dark = {str(element_id): 0 for element_id in LIGHTING_ELEMENT_IDS}
        check(stand.snapshot()["physical_lighting"] == dark, "startup did not leave all 18 lighting channels off")

        lifted = await stand.inject_physical({"type": "SpaceReleased", "_source": "clean_process_smoke", "_sequence": 1})
        check(lifted.get("reason") == "exhibition_handset_lifted", "SpaceReleased missed canonical ingress")
        check(sum(item.get("type") == "lighting_wave_intent" for item in lifted.get("outputs", [])) == 1, "wave was not emitted once")
        check(sum(item.get("type") == "welcome_timer_scheduled" for item in lifted.get("outputs", [])) == 1, "welcome timer was not scheduled once")

        clock[0] = 105.0
        due = await stand.application.flush_welcome_timers()
        check(len(due) == 1, "welcome deadline did not progress deterministically")
        check(sum(item.get("phrase_id") == "PHRASE_001" for item in due[0].get("outputs", [])) == 1, "welcome was not emitted once")

        placed = await stand.inject_physical({"type": "SpacePressed", "_source": "clean_process_smoke", "_sequence": 2})
        check(placed.get("reason") == "exhibition_reset", "SpacePressed did not reset")
        check(stand.application.canonical_runtime.sessions == {}, "reset left a canonical session")
        reset_health = stand.health_snapshot()
        check(reset_health["canonical_state"] == "idle" and reset_health["handset"] == "down", "reset did not return to standby")
        check(stand.snapshot()["physical_lighting"] == dark, "reset did not turn all 18 lighting channels off")

        return {
            "status": "ok",
            "health_status": health["status"],
            "startup": {"state": health["canonical_state"], "handset": health["handset"]},
            "trace": [
                "startup", "handset_down", "dark_standby", "SpaceReleased",
                "HandsetLifted", "wave", "welcome_timer", "welcome",
                "SpacePressed", "full_reset", "shutdown",
            ],
            "personal_data_output": False,
            "acceptance": "synthetic_simulator_only",
        }
    finally:
        stand.close()
        stand.close()


async def main() -> None:
    result = await asyncio.wait_for(scenario(), timeout=15.0)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_code": type(exc).__name__, "message": str(exc)[:240]}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
