"""Concrete canonical policies used by the Mishka application boundary."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any, Mapping

from .canonical_contract import PERSONA_FORBIDDEN, VoiceManifest
from .schedule_weather import ScheduleWeatherProviderError, validate_report
from .lighting import (
    BUTTON_DIGIT_ELEMENTS, BUTTON_FILL_ELEMENTS, BUTTON_OUTPUT_ELEMENTS,
    COMPENSATION_ELEMENTS, PICTOGRAM_ELEMENTS, LIGHTING_ELEMENT_IDS,
)


def _button_lights(*button_ids: int, digits: bool = False, pictograms: bool = False) -> tuple[tuple[int, int], ...]:
    """Expand logical buttons to flat channels, including conditional fillers."""
    commands: list[tuple[int, int]] = []
    for button_id in button_ids:
        commands.append((BUTTON_FILL_ELEMENTS[button_id], 1))
        if digits:
            commands.append((BUTTON_DIGIT_ELEMENTS[button_id][1], 1))
        if button_id in PICTOGRAM_ELEMENTS:
            element_id = PICTOGRAM_ELEMENTS[button_id] if pictograms else COMPENSATION_ELEMENTS[button_id]
            commands.append((element_id, 1))
    return tuple(commands)

LED_PRESETS = {
    # READY is deliberately attract-light only; menu controls are enabled
    # by ROOT_MENU after the physical session has entered the menu.
    "READY": ((14, 1),),
    "ALL_ON": tuple((element_id, 1) for element_id in LIGHTING_ELEMENT_IDS),
    "ROOT_MENU": _button_lights(1, 2, 3, 4, 5, 6, 7),
    "GAME_MENU": _button_lights(1, 2, 3, 4, 5, 6, 7),
    # Story chooser: all digits remain visible; only fills 6/7 are on.
    "STORY_MENU": tuple((BUTTON_DIGIT_ELEMENTS[button][1], 1) for button in range(1, 8)) + ((BUTTON_FILL_ELEMENTS[6], 1), (BUTTON_FILL_ELEMENTS[7], 1)),
    "STORY_PLAYBACK": ((14, 1),),
    "SUPPORT": ((2, 1), (14, 1)),
    "PERSONAL_STORY": ((3, 1), (14, 1)),
    "BEAR_DIALOGUE": ((14, 1),),
    "SCHEDULE_WEATHER": ((6, 1), (15, 1)),
    "MOOD_CHOICES": _button_lights(1, 2, 3, 4, 5, 6, 7),
    "LISTENING": ((14, 1),),
    "EXHIBITION_AGE_CHOICES": _button_lights(1, 2, 3),
    "EXHIBITION_MENU_TUTORIAL": (),
    "THINKING": ((14, 1),),
    "PAUSED": _button_lights(6, 7, digits=True),
    "RETRY": (),
    "HINT": (),
    # HELP is the non-verbal yes/no contract: 1 = no, 7 = yes/help.
    "HELP": _button_lights(1, 7),
    "SAFE_STOP": ((14, 1),),
    "SUCCESS": ((0, 1), (8, 1)),
    "ERROR_RECOVERABLE": ((14, 1), (6, 1), (13, 1), (17, 1)),
}
BUTTON_ELEMENTS = dict(BUTTON_OUTPUT_ELEMENTS)

@dataclass(frozen=True)
class SafetyClassification:
    severity: str
    reason: str

class SafetyClassifier:
    """Deterministic first layer; semantic classifiers may only raise severity."""
    URGENT = re.compile(r"(кровь|не могу дышать|не могу двиг|потерялся|потерялась|опасно|пожар|ударил|трогает|насили|умер|убью|опасный взрослый|срочно)", re.I)
    CONCERN = re.compile(r"(страшно|обид|плачу|плох[оий]|болит|больно|секрет|не говори взрослым|не хочу идти)", re.I)

    def classify(self, text: str, semantic: str = "none") -> SafetyClassification:
        deterministic = "urgent" if self.URGENT.search(text) else "concern" if self.CONCERN.search(text) else "none"
        levels = {"none": 0, "concern": 1, "urgent": 2}
        semantic = semantic if semantic in levels else "none"
        severity = deterministic if levels[deterministic] >= levels[semantic] else semantic
        return SafetyClassification(severity, "deterministic" if severity == deterministic else "semantic")

def classify_intent(text: str) -> str:
    value = text.casefold().strip()
    if re.search(r"(поставь|поставить|на)\s*пауз|пауза", value): return "pause_story"
    if re.search(r"(продолжи|продолжить|дальше|возобнови)", value): return "resume_story"
    if re.search(r"\b(стоп|хватит|закончить сказку|выключи сказку)\b", value): return "stop_story"
    if re.search(r"(вау|класс|здорово|смешно|ха-ха|ура|ух ты)", value): return "emotional_reaction"
    if re.search(r"(настроени|груст|рад|весел)", value): return "mood"
    if re.search(r"(куда|маршрут|идти)", value): return "where_now"
    if re.search(r"(позвать взросл|нужен взросл|помоги)", value): return "request_adult"
    if re.search(r"(болит|страшно|обид|жалоб)", value): return "support"
    theme_map = (
        (r"динозавр", "dinosaurs"), (r"космос|звезд", "космос"),
        (r"лес", "лес"), (r"машин", "дружелюбные машины"),
        (r"мор|океан", "море"), (r"мастерск", "волшебная мастерская"),
        (r"ящериц", "original_gentle_lizard"),
    )
    for pattern, theme in theme_map:
        if re.search(pattern, value): return theme
    if re.search(r"^(да|ага|угу|конечно|хочу|повтори)([.!?, ]|$)", value): return "yes"
    if re.search(r"^(нет|неа|не хочу|не надо|не нужно|не повторяй)([.!?, ]|$)", value): return "no"
    if re.search(r"(сказк|истори)", value): return "story_theme"
    return "free_speech"

def lint_persona(text: str) -> tuple[bool, str | None]:
    if not isinstance(text, str) or not text.strip(): return False, "empty"
    if PERSONA_FORBIDDEN.search(text): return False, "technical_or_patronizing_word"
    return True, None

class RecordedPhraseResolver:
    """Only exact approved WAV assets may satisfy recorded phrase effects."""
    def __init__(self, manifest_path: str, *, allow_development_assets: bool = False) -> None:
        self.manifest = VoiceManifest(manifest_path, allow_development_assets=allow_development_assets)

    def resolve(self, phrase_id: str, content_hash: str = "") -> dict[str, Any] | None:
        return self.manifest.resolve(phrase_id, content_hash)

    def readiness(self) -> dict[str, Any]: return self.manifest.readiness()

    def diagnose(self, phrase_id: str, content_hash: str = "") -> dict[str, Any]:
        return self.manifest.diagnose(phrase_id, content_hash)

class SimulatedAdultHandoff:
    """Development-only outbox that exercises the whole safety acknowledgement path."""

    def __init__(self) -> None:
        self.outbox: list[dict[str, Any]] = []

    async def request(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        item = dict(payload)
        item["transport"] = "simulated_local_outbox"
        item["simulated"] = True
        item["delivery"] = "acknowledged"
        self.outbox.append(item)
        return {
            "acknowledged": True,
            "ack_id": f"simulated-{len(self.outbox)}",
            "transport": "simulated_local_outbox",
            "simulated": True,
        }

class TypedInfoRenderer:
    """Render only typed provider facts; it never asks an LLM for information."""
    def __init__(self, service: Any, profile: Mapping[str, Any] | None, tag_id: str | None) -> None:
        self.service, self.profile, self.tag_id = service, profile, tag_id

    @staticmethod
    def _schedule_text(item: Mapping[str, Any], *, include_location: bool) -> str:
        text = f"{item['time']} — {item['title']}"
        if include_location:
            if item.get("room"):
                text += f", кабинет {item['room']}"
            if item.get("floor") is not None:
                text += f", {item['floor']} этаж"
            if item.get("minutes_until") is not None and int(item["minutes_until"]) >= 0:
                text += f", через {item['minutes_until']} минут"
        return text

    async def render(self, kind: str, *, allow_demo: bool = False) -> dict[str, Any]:
        if kind == "date":
            try:
                timezone = ZoneInfo(str((self.profile or {}).get("timezone") or "Europe/Moscow"))
            except Exception:
                timezone = ZoneInfo("Europe/Moscow")
            current = datetime.now(timezone)
            months = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря")
            return {"kind": "date", "text": f"Сегодня {current.day} {months[current.month - 1]} {current.year} года."}
        if kind not in {"weather", "what_next", "where_now"}:
            raise ValueError("unknown typed information kind")
        if self.service is None or not isinstance(self.profile, Mapping):
            raise ScheduleWeatherProviderError("typed_information_unavailable")
        # Production services expose independent typed calls so a weather
        # outage does not make the child's schedule unavailable, and vice versa.
        if kind == "weather" and callable(getattr(self.service, "weather", None)):
            weather = await self.service.weather(dict(self.profile), self.tag_id or "", allow_demo=allow_demo)
            return {"kind": kind, "text": weather["summary"], "source": weather["source"]}
        if kind in {"what_next", "where_now"} and callable(getattr(self.service, "schedule", None)):
            schedule = await self.service.schedule(dict(self.profile), self.tag_id or "")
            item = schedule[0 if kind == "what_next" else -1]
            return {"kind": kind, "text": self._schedule_text(item, include_location=kind == "where_now")}
        report = await self.service.build(dict(self.profile), self.tag_id or "")
        report = validate_report(report, allow_demo=allow_demo)
        if kind == "weather":
            return {"kind": kind, "text": report["weather"]["summary"], "source": report["source"]}
        item = report["schedule"][0 if kind == "what_next" else -1]
        return {"kind": kind, "text": self._schedule_text(item, include_location=kind == "where_now"), "source": report["source"]}


class CanonicalContentCatalog:
    """Single content boundary: one validated scenario and immutable manifests."""
    STORY_IDS = (
        "fixed_aibolit_v1", "fixed_squirrel_v1", "fixed_lukomorye_v1",
        "fixed_soft_paws_star_v1", "fixed_neznayka_v1",
    )

    def __init__(self, root: str, *, allow_development_assets: bool = False) -> None:
        from pathlib import Path
        from .canonical_contract import FixedStoryManifest, GAME_CATALOG, canonical_state_nodes
        from app.scenarios.canonical_graph import load_canonical_scenario_document
        self.root = Path(root)
        self.allow_development_assets = bool(allow_development_assets)
        self.voice = VoiceManifest(
            self.root / "audio" / "voice_manifest.json",
            allow_development_assets=self.allow_development_assets,
        )
        self.stories = FixedStoryManifest(self.root / "stories" / "fixed_manifest.json", self.root.parent)
        self.nodes = canonical_state_nodes()
        self.games = {str(item["game_id"]): dict(item) for item in GAME_CATALOG}
        self._report_cache: dict[str, Any] | None = None
        self.scenario = load_canonical_scenario_document(
            self.root / "scenarios" / "canonical_product_v1.json"
        )

    @property
    def has_errors(self) -> bool:
        return not self.voice.readiness()["ready"] or set(self.scenario.get("states", {})) != set(self.nodes)

    def report(self) -> dict[str, Any]:
        if self._report_cache is not None:
            return dict(self._report_cache)
        fixed = {story_id: bool(self.stories.get_verified(story_id)) for story_id in self.STORY_IDS}
        placeholder = bool(self.stories.get_development_placeholder()) if self.allow_development_assets else False
        effective_fixed = all(fixed.values()) or (self.allow_development_assets and placeholder)
        result = {
            "contract": self.scenario.get("contract"),
            "scenario_id": self.scenario.get("scenario_id"),
            "scenario_version": self.scenario.get("version"),
            "langgraph_authorization": self.scenario.get("langgraph_authorization") is True,
            "state_nodes": len(self.nodes),
            "voice": self.voice.readiness(),
            "fixed_stories": fixed,
            "development_placeholder_available": placeholder,
            "ready": not self.has_errors and effective_fixed,
        }
        self._report_cache = dict(result)
        return result

    def get(self, scenario_id: str) -> dict[str, Any]:
        if str(scenario_id) != str(self.scenario.get("scenario_id")):
            raise KeyError(f"unknown canonical scenario: {scenario_id}")
        return dict(self.scenario)

    def game(self, game_id: str) -> dict[str, Any] | None:
        item = self.games.get(str(game_id))
        return dict(item) if item is not None else None

    def story(self, story_id: str) -> dict[str, Any] | None:
        verified = self.stories.get_verified(str(story_id))
        if verified is not None:
            return verified
        if self.allow_development_assets and self.stories.get_development_placeholder() is not None:
            item = self.stories.stories.get(str(story_id))
            return dict(item) if isinstance(item, dict) else None
        return None

