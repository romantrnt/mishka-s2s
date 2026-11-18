"""Concrete canonical policies used by the Mishka application boundary."""
from __future__ import annotations

import re
import math
from dataclasses import dataclass
from datetime import date, datetime
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

# Shared with the existing conversational listening/output renderer.
# These are logical roles, not the pre-remap flat channel 14.
DIALOGUE_FILL_ELEMENTS = tuple(BUTTON_FILL_ELEMENTS[button] for button in range(1, 8))
DIALOGUE_ANCHOR_ELEMENTS = (PICTOGRAM_ELEMENTS[6], BUTTON_DIGIT_ELEMENTS[7][1])

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
    "GAME_OFFER": _button_lights(1, 7, digits=True),
    "SCHEDULE_WEATHER": ((6, 1), (15, 1)),
    "MOOD_CHOICES": _button_lights(1, 2, 3, 4, 5, 6, 7),
    "LISTENING": tuple((element, 1) for element in DIALOGUE_FILL_ELEMENTS + DIALOGUE_ANCHOR_ELEMENTS),
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

def wellbeing_reassurance(text: str, question: str | None) -> bool:
    patterns = {"pain_or_discomfort": r"(?:нет,?\s+ничего\s+не\s+болит|вс[её]\s+хорошо,\s+у\s+меня\s+ничего\s+не\s+болит)",
                "fear": r"нет,?\s+мне\s+не\s+страшно"}
    pattern = patterns.get(question)
    return bool(pattern and re.fullmatch(r"\s*" + pattern + r"\s*[.!]*\s*", text, re.I))


class SafetyClassifier:
    """Deterministic first layer; semantic classifiers may only raise severity."""
    # Whole lexical forms, not substrings: безопасно / умеренная / пожарный
    # are not disclosures merely because they contain an alarming stem.
    URGENT = re.compile(r"""\b(?:
        кровь|кровью|не\s+могу\s+дышать|не\s+могу\s+двиг[а-яё]*|
        не\s+хватает\s+воздуха|воздуха\s+не\s+хватает|
        мне\s+(?:трудно|тяжело)\s+дышать|я\s+задыхаюсь|^задыхаюсь|
        потерял(?:ся|ась)|опасн[а-яё]*|
        пожар(?:а|у|ом|е|ы|ов|ам|ами|ах|ищ[а-яё]*)?|пожарн(?:ая|ой|ую)\s+тревог[а-яё]*|
        ударил[а-яё]*|трогает[а-яё]*|насили[а-яё]*|
        умер(?:ла|ло|ли|еть|ш[а-яё]*)?|умертв[а-яё]*|умерщв[а-яё]*|убью(?:т|сь)?|срочно|
        не\s+хочу\s+жить|меня\s+бьют
    )\b""", re.I | re.X)
    CONCERN = re.compile(r"""\b(?:
        страшно|обид(?:а|у|е|ы|ой|ою|ам|ами|ах|н[а-яё]+|
                      ел[а-яё]*|еть(?:ся)?|ит[а-яё]*|ишь[а-яё]*|им[а-яё]*|
                      ят[а-яё]*|чи[кв][а-яё]*)|
        (?:много|полно|столько|от|из-за)\s+обид|
        плачу|плох(?:о|ой|ий|ие|им|ими|их|ому|ого|ую|ая|ое)|
        болит|больно|секрет[а-яё]*|не\s+говори\s+взрослым|не\s+хочу\s+идти
    )\b""", re.I | re.X)
    # This is deliberately not a general negation rule. Only a complete,
    # unambiguous reassurance is exempt; extra clauses still get a full scan.
    # Semantic classification can raise even this narrowly exempted input.
    NOT_LOST = re.compile(r"\s*(?:я\s+)?не\s+потерял(?:ся|ась)\s*[.!]*\s*", re.I)

    def classify(self, text: str, semantic: str = "none", *, wellbeing_question: str | None = None) -> SafetyClassification:
        deterministic = ("none" if self.NOT_LOST.fullmatch(text) or wellbeing_reassurance(text, wellbeing_question) else
                         "urgent" if self.URGENT.search(text.strip()) else
                         "concern" if self.CONCERN.search(text) else "none")
        levels = {"none": 0, "concern": 1, "urgent": 2}
        semantic = semantic if semantic in levels else "none"
        severity = deterministic if levels[deterministic] >= levels[semantic] else semantic
        return SafetyClassification(severity, "deterministic" if severity == deterministic else "semantic")

# Only a direct command, not a parent mentioned in a story or an advice query.
# This intent requests a local safety pause and nearby-adult guidance.
_CAREGIVER_REQUEST = re.compile(r"""
    (?:(?:мишка|пожалуйста)[,\s]+)*
    (?:позови(?:те)?(?:\s+|,\s*пожалуйста[,\s]+)(?:пожалуйста[,\s]+)?(?:маму|папу)
       |попроси(?:те)?(?:\s+|,\s*пожалуйста[,\s]+)(?:пожалуйста[,\s]+)?(?:маму|папу)\s+подойти)
    (?:\s+(?:сюда|ко\s+мне))?
    (?:[,\s]+пожалуйста)?[.!?]*
""", re.I | re.X)


def classify_intent(text: str) -> str:
    value = text.casefold().strip()
    if _CAREGIVER_REQUEST.fullmatch(value): return "help_request"
    # An explicit request for an adult outranks ordinary navigation/mood.
    if re.search(r"(позвать\s+взросл|позови(?:те)?\s+взросл|нужен\s+взросл)", value): return "help_request"
    if re.search(r"(поставь|поставить|на)\s*пауз|пауза", value): return "pause_story"
    if re.search(r"(продолжи|продолжить|дальше|возобнови)", value): return "resume_story"
    if re.search(r"\b(стоп|хватит|закончить сказку|выключи сказку|останови сказку)\b", value): return "stop_story"
    if re.search(r"(вау|класс|здорово|смешно|ха-ха|ура|ух ты)", value): return "emotional_reaction"
    if re.search(r"(настроени|груст|рад|весел)", value): return "mood"
    if re.search(r"(куда|маршрут|идти)", value): return "where_now"
    if re.search(r"помоги", value): return "help_request"
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


def game_confirmation_intent(text: str) -> str:
    """Only a complete answer confirms the current owned game proposal."""
    value = " ".join(text.casefold().split()).strip(" .!?…,")
    positive = r"(?:да|ага|угу|конечно|хорошо|ладно|согласен|согласна|давай(?: сыграем| поиграем)?|хочу(?: играть| поиграть)?|поиграем|запускай)"
    if re.fullmatch(rf"(?:ну\s+)?{positive}(?:(?:\s*,\s*|\s+(?:и\s+)?){positive}){{0,2}}(?:,? пожалуйста)?", value):
        return "yes"
    if re.fullmatch(r"(?:нет|неа|не хочу|не надо|не нужно|отмена|передумал|передумала)(?:,? спасибо)?", value):
        return "no"
    return "free_speech"


def day_information_intent(text: str) -> str | None:
    """Context-only requests; facts always come from the typed provider."""
    value = text.casefold()
    explicit = conversational_information_intent(text)
    if explicit:
        return explicit
    if re.search(r"погод|температур|на улице|дожд|снег", value):
        return "weather"
    if re.search(r"какое?\s+(?:сегодня\s+)?число|дат[ауы]|день\s+недел|какой\s+(?:сегодня\s+)?день", value):
        return "date"
    if re.search(r"что\s+(?:будет\s+)?(?:потом|дальше)|следующ|после\s+этого", value):
        return "what_next"
    if re.search(r"куда|где|сейчас|текуще", value):
        return "where_now"
    return None


def conversational_information_intent(text: str) -> str | None:
    """Explicit factual subject; character/location talk is ordinary dialogue."""
    value = ' '.join(text.casefold().split())
    value = re.sub(r'^(?:(?:мишка|пожалуйста)[, ]+)*(?:я )?не (?:знаю|помню|могу вспомнить)[,:]?\s+', '', value)
    if re.search(r'\b(?:не|нет)\b', value):
        return None
    # A factual noun inside play, a story or Mishka's fictional world is
    # not a request for the child's local provider facts.
    if re.search(r'\b(?:игр\w*|поигра\w*|сыгра\w*|квиз\w*|викторин\w*|сказк\w*|истори\w*|волшебн\w*|сказочн\w*)\b', value):
        return None
    if re.search(r'\b(?:тво\w*|у тебя)\b', value):
        return None
    if re.search(r'\b(?:почему|зачем|что такое|как (?:образуется|меняется|устроен[ао]?))\b', value):
        return None
    if re.search(r'погод|температур[ауы]|(?:нужно|надо|что|как)\s+(?:мне\s+)?(?:надеть|одеться)|на улице (?:дождь|снег|холодно|тепло)', value):
        return 'weather'
    if re.search(r'какое?\s+(?:сегодня\s+)?число|какая\s+(?:сегодня\s+)?дата|какой\s+(?:сегодня\s+)?день|день\s+недел', value):
        return 'date'
    child = bool(re.search(r'\b(?:мне|меня|мной|мой|моя|мое|моё|мои|моим|моего|моей|моему|моими|моих|мы|нам|нас|я)\b', value))
    appointment = bool(re.search(r'заняти[еяюй]|расписани|кабинет|врач|прием|приём|доктор|процедур', value))
    if child and re.search(r'куда\s+(?:мне|нам)\s+(?:сейчас\s+)?идти', value):
        return 'where_now'
    request = bool(re.search(r'\b(?:кто|кого|кому|кем|какой|какие|какая|какому|каким|какого|какую|каких|когда|куда|где|как|сколько|что|чего|чему|чем|покажи|скажи|расскажи|узнать)\b', value))
    if child and appointment and request:
        if re.search(r'\b(?:кто|какой|какие)\b', value) and re.search(r'врач|доктор', value):
            return 'card_doctors'
        return 'what_next' if re.search(r'следующ|потом|дальше|после|расписани|когда', value) else 'where_now'
    return None

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

class TypedInfoRenderer:
    """Render only typed provider facts; it never asks an LLM for information."""
    def __init__(self, service: Any, profile: Mapping[str, Any] | None, schedule_ref: str | None, *, now=None) -> None:
        self.service, self.profile, self.schedule_ref = service, profile, schedule_ref
        zone = ZoneInfo(str((profile or {}).get("timezone") or "Europe/Moscow"))
        self.now = now or (lambda: datetime.now(zone))

    def _select_schedule_item(self, schedule, kind):
        # Array order, a bare HH:MM and an arbitrary last item are not authority
        # to tell a child where to go now. Require explicit dated provider facts.
        now = self.now()
        if now.utcoffset() is None:
            raise ScheduleWeatherProviderError("schedule_clock_unavailable")
        candidates = []
        for item in schedule:
            if not isinstance(item, Mapping):
                continue
            status = item.get("status")
            if status not in ({"in_progress"} if kind == "where_now" else {"scheduled"}):
                continue
            try:
                start = datetime.fromisoformat(str(item.get("start_at") or "").replace("Z", "+00:00"))
            except ValueError:
                continue
            if start.utcoffset() is None:
                continue
            if kind == "what_next" and start < now:
                continue
            if kind == "where_now" and (start > now or start.astimezone(now.tzinfo).date() != now.date()):
                continue
            candidates.append((start, item))
        candidates.sort(key=lambda pair: pair[0])
        if not candidates or (kind == "where_now" and len(candidates) != 1) or (len(candidates)>1 and candidates[0][0]==candidates[1][0]):
            raise ScheduleWeatherProviderError("schedule_current_item_unavailable")
        return candidates[0][1]

    def _schedule_text(self, item: Mapping[str, Any], *, include_location: bool) -> str:
        now = self.now()
        start = datetime.fromisoformat(str(item["start_at"]).replace("Z", "+00:00")).astimezone(now.tzinfo)
        day = "Сегодня" if start.date() == now.date() else start.strftime("%d.%m.%Y")
        # Use the same dated instant used for selection, not a stale/conflicting
        # HH:MM or provider countdown. A future-day appointment must be labelled.
        text = f"{day} в {start:%H:%M} — {item['title']}"
        if include_location:
            if item.get("room"):
                text += f", кабинет {item['room']}"
            if item.get("floor") is not None:
                text += f", {item['floor']} этаж"
        return text

    def _card_doctors_text(self):
        profile = self.profile or {}
        names, visit = profile.get('card_doctor_names'), profile.get('card_visit_date')
        if (profile.get('card_doctors_source') != 'data14.v1' or not isinstance(names, list)
                or not 1 <= len(names) <= 32 or not all(isinstance(name, str) and 1 <= len(name) <= 160 for name in names)
                or not isinstance(visit, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', visit)):
            raise ScheduleWeatherProviderError('card_doctors_unavailable')
        try:
            visit_date = date.fromisoformat(visit)
        except ValueError:
            raise ScheduleWeatherProviderError('card_doctors_unavailable') from None
        current = self.now().astimezone(ZoneInfo('Europe/Moscow')).date()
        label = 'сегодня' if visit_date == current else visit_date.strftime('%d.%m.%Y')
        preview = ', '.join(names[:2])
        partial = ' Это часть списка.' if len(names) > 2 else ''
        return f'В твоей карточке на {label} указаны: {preview}.{partial} Точного плана, времени и кабинета у меня нет.'

    @staticmethod
    def _weather_text(weather):
        text = str(weather['summary']).strip().rstrip('.!') + '.'
        temperature = weather.get('temperature_c')
        if type(temperature) not in (int, float) or not math.isfinite(temperature) or not -100 <= temperature <= 70:
            return text
        clothes = ('Для прогулки пригодятся тёплая куртка и шапка.' if temperature <= 5 else
                   'На прогулку можно взять куртку.' if temperature <= 15 else
                   'Для прогулки подойдёт лёгкая одежда.')
        condition = weather.get('condition')
        rain = ' Если пойдёшь гулять, пригодится дождевик.' if condition in {
            'drizzle','light-rain','rain','moderate-rain','heavy-rain','continuous-heavy-rain',
            'showers','wet-snow','thunderstorm-with-rain','thunderstorm-with-hail'} else ''
        return text + ' ' + clothes + rain

    async def render(self, kind: str, *, allow_demo: bool = False) -> dict[str, Any]:
        if kind == "menu":
            return {"kind": "menu", "text": "Один — дата. Два — погода. Три — планы. Четыре — куда идти. Семь — назад."}
        if kind == "date":
            try:
                timezone = ZoneInfo(str((self.profile or {}).get("timezone") or "Europe/Moscow"))
            except Exception:
                timezone = ZoneInfo("Europe/Moscow")
            current = self.now().astimezone(timezone)
            months = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря")
            return {"kind": "date", "text": f"Сегодня {current.day} {months[current.month - 1]} {current.year} года."}
        if kind == 'card_doctors':
            return {'kind':kind, 'text':self._card_doctors_text()}
        if kind not in {"weather", "what_next", "where_now"}:
            raise ValueError("unknown typed information kind")
        if self.service is None or (kind != "weather" and not isinstance(self.profile, Mapping)):
            raise ScheduleWeatherProviderError("typed_information_unavailable")
        # Production services expose independent typed calls so a weather
        # outage does not make the child's schedule unavailable, and vice versa.
        if kind == "weather" and callable(getattr(self.service, "weather", None)):
            weather = await self.service.weather(dict(self.profile or {}), self.schedule_ref or "", allow_demo=allow_demo)
            return {"kind": kind, "text": self._weather_text(weather), "source": weather["source"]}
        if kind in {"what_next", "where_now"} and callable(getattr(self.service, "schedule", None)):
            schedule = await self.service.schedule(dict(self.profile), self.schedule_ref or "")
            item = self._select_schedule_item(schedule, kind)
            return {"kind": kind, "text": self._schedule_text(item, include_location=kind == "where_now")}
        report = await self.service.build(dict(self.profile or {}), self.schedule_ref or "")
        report = validate_report(report, allow_demo=allow_demo)
        if kind == "weather":
            return {"kind": kind, "text": self._weather_text(report["weather"]), "source": report["source"]}
        item = self._select_schedule_item(report["schedule"], kind)
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
