"""Fail-closed canonical product contract for the Mishka stand.

This module deliberately has no FastAPI, GPIO, serial, audio or LLM imports.
It authorizes transitions and returns declarative effects; the application layer
owns the actual hardware/provider calls.  It is safe to use headless.
"""
from __future__ import annotations

import hashlib
import json
import math
import struct
import wave
import re
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from .game_engine import GameState, AVAILABLE_GAME_SPECS, start as start_game, advance as advance_game, desired_buttons, pause as pause_game, arm_wait, next_deadline, timer_step, begin_demonstration, IMMERSIVE_GAME_SPECS, current_challenge, game_answer_buttons, finish_prompt, hold_exit_policy, cancel_exit_gesture
from .age_policy import age_details, policy_for, policy_for_band
from .voice_catalog import load_phrase_catalog

RAM_TTL_SECONDS = 300
EXHIBITION_WELCOME_DELAY_SECONDS = 5.0
AGE_BANDS = ("1-3", "3-5", "5-7")
BUTTON_MODES = {
    1: "game_menu", 2: "fixed_story_menu", 3: "support_intake",
    4: "personal_story_theme", 5: "wellbeing_check", 6: "bear_dialogue",
    7: "schedule_weather",
}
LIGHTING_ELEMENT_IDS = tuple(range(18))
GAME_CATALOG = (
    {"game_id": "catch_light", "level": 0, "input": "button"},
    {"game_id": "find_pair", "level": 2, "input": "button"},
    {"game_id": "press_together", "level": 3, "input": "simultaneous"},
    {"game_id": "ladder_forward", "level": 4, "input": "button"},
    {"game_id": "ladder_reverse", "level": 5, "input": "button"},
    {"game_id": "repeat_pattern", "level": 6, "input": "button"},
    {"game_id": "hold_light", "level": 7, "input": "hold"},
    {"game_id": "dog_legs", "level": 8, "input": "button"},
    {"game_id": "seasons", "level": 8, "input": "button"},
    {"game_id": "week_days", "level": 8, "input": "button"},
    {"game_id": "missing_number", "level": 9, "input": "button"},
    {"game_id": "continue_series", "level": 9, "input": "button"},
    {"game_id": "free_color_music", "level": 0, "input": "button"},
)
STATE_NAMES = frozenset({
    "idle", "profile_ready", "anonymous_visual", "visual_welcome",
    "awaiting_handset", "voice_welcome", "personal_welcome_listen", "exhibition_introduction",
    "exhibition_name_question", "exhibition_name_capture", "exhibition_age_prompt",
    "exhibition_age_hint", "exhibition_age_selection", "exhibition_age_complete",
    "exhibition_onboarding_complete",
    "exhibition_menu_tutorial_intro", "exhibition_menu_tutorial_prompt",
    "exhibition_menu_tutorial_buttons", "exhibition_menu_wave",
    "exhibition_menu_repeat_prompt", "exhibition_menu_repeat_listen",
    "exhibition_menu_replay_buttons", "menu_ready",
    "automatic_mood_check", "root_menu",
    "dialogue_suspended_ram", "dialogue_memory_expired", "game_menu", "game_active",
    "fixed_story_menu", "fixed_story_playing", "fixed_story_choice_wave",
    "story_speech_analysis", "story_paused", "story_pause_announcement",
    "story_resume_announcement", "story_suspended_ram", "support_intake", "schedule_navigation",
    "mood_report", "complaint_intake", "personal_story_theme",
    "personal_story_generating", "personal_story_validating", "personal_story_playing",
    "wellbeing_check", "safety_stop", "bear_dialogue", "schedule_weather",
    "soft_end", "session_closed",
})
ALLOWED_EVENTS = frozenset({
    "ClientProfileReady", "ClientProfileCleared", "IdentityReadyExpired", "IdentityAuthorizationExpired",
    "AnonymousStart", "HandsetLifted", "HandsetPlaced",
    "ButtonPressed", "ButtonReleased", "ButtonHeld", "ButtonLongHeld", "SpeechStarted",
    "SpeechEnded", "VoiceIntentReady", "AudioStarted", "AudioCompleted", "AudioFailed",
    "PersonalStoryReady", "StoryPositionUpdated", "DialogueFallback", "TimeoutFired", "WelcomeTimerFired", "GameTimer", "HardwareDisconnected", "HardwareReconnected",
    "SessionStarted", "InterviewSkipped", "SessionCancel", "SessionReset", "InteractionIdleExpired",
    "ExhibitionOnboardingComplete", "ExhibitionMenuWaveComplete", "StoryChoiceWaveComplete", "MenuTutorialStep",
})
# Buttons 5 (help) and 7 (exit) preempt game scoring.
GAME_PLAYABLE_TARGETS = (1, 2, 3, 4, 6)


def _game_target_for_level(level: int) -> int:
    return GAME_PLAYABLE_TARGETS[(level * 3) % len(GAME_PLAYABLE_TARGETS)]


PERSONA_FORBIDDEN = re.compile(
    r"\b(малыш|llm|tts|stt|vad|rfid|sqlite|api|langgraph|prompt|backend|state|tool|id)\b",
    re.IGNORECASE,
)
ALLOWED_STORY_THEMES = {"dinosaurs", "космос", "space", "лес", "forest", "дружелюбные машины", "friendly_machines", "море", "sea", "волшебная мастерская", "workshop", "original_gentle_lizard"}

STORY_FORBIDDEN = re.compile(
    r"\b(годзилл\w*|айболит\w*|незнайк\w*|лукомор\w*|мягк(?:ие|их)\s+лапк\w*|"
    r"кровь|оружи\w*|похищ\w*|убий\w*|секрет\w* от взрослых|"
    r"лекарств\w*|диагноз\w*|врач\w*|расписан\w*|кабинет\w*|умр[её]т)\b", re.IGNORECASE,
)


def _utc_now() -> float:
    return time.time()


def _opaque_profile_ref(profile: Mapping[str, Any] | None) -> str | None:
    if not profile:
        return None
    candidate = profile.get("profile_ref") or profile.get("child_id")
    if not candidate:
        return None
    return str(candidate)


ALLOWED_GENDERS = {"masculine", "feminine", "neutral"}
ALLOWED_TRIGGER_HANDLING = {"avoid_parent_assumptions", "avoid_body_assumptions", "adult_neutral"}
ALLOWED_TRIGGER_TOPICS = {
    "parent_absence", "limb_absence_or_difference", "separation", "loss",
    "medical_procedure_fear", "body_vulnerability", "bullying", "animal_fear",
    "darkness_fear", "loud_sound_fear",
}


def accessibility_policy_from_profile(profile: Mapping[str, Any] | None) -> dict[str, Any]:
    """Reduce profile data to non-sensitive controller constraints."""
    value = profile or {}
    communication = value.get("communication_flags") if isinstance(value.get("communication_flags"), list) else []
    sensory = value.get("sensory_flags") if isinstance(value.get("sensory_flags"), list) else []
    motor = value.get("motor_flags") if isinstance(value.get("motor_flags"), list) else []
    triggers = []
    for item in value.get("trigger_topics", ()) if isinstance(value.get("trigger_topics"), list) else ():
        if not isinstance(item, Mapping) or item.get("status") != "active":
            continue
        topic_id = str(item.get("topic_id") or "")
        if topic_id not in ALLOWED_TRIGGER_TOPICS:
            continue
        handling = str(item.get("handling") or "adult_neutral")
        for token in re.split(r"[;,]", handling):
            token = token.strip()
            if token in ALLOWED_TRIGGER_HANDLING:
                triggers.append(token)
    return {"communication": tuple(str(x)[:40] for x in communication[:8]),
            "sensory": tuple(str(x)[:40] for x in sensory[:8]),
            "motor": tuple(str(x)[:40] for x in motor[:8]),
            "content_constraints": tuple(sorted(set(triggers)))}


def age_band_from_profile(profile: Mapping[str, Any] | None, *, now: datetime | None = None) -> tuple[str, str]:
    """Derive an operational band from a complete DOB; never guess sex/name."""
    if (type((profile or {}).get("age_years")) is int and 0 <= profile["age_years"] <= 130
            and profile.get("age_source") == "data14_age_years"):
        years = profile["age_years"]
        return ("1-3" if years <= 3 else "3-5" if years <= 5 else "5-7"), "data14_age_years"
    raw = (profile or {}).get("date_of_birth")
    if not isinstance(raw, str):
        return "3-5", "profile_missing_default" if profile else "anonymous_default"
    try:
        born = date.fromisoformat(raw)
        timezone = ZoneInfo(str((profile or {}).get("timezone") or "Europe/Moscow"))
        today = (now.astimezone(timezone).date() if now else datetime.now(timezone).date())
    except (ValueError, TypeError, KeyError):
        return "3-5", "profile_missing_default"
    if born > today:
        return "3-5", "profile_missing_default"
    years = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    return ("1-3" if years <= 3 else "3-5" if years <= 5 else "5-7"), "profile_dob"


@dataclass(frozen=True)
class CanonicalEvent:
    event_type: str
    session_id: str
    sequence: int
    state_version: int
    source: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    correlation_id: str = ""
    timestamp: float = 0.0
    monotonic_timestamp: float | None = None

    def validate(self) -> None:
        if 'cancelled' in self.payload and (self.event_type != 'ButtonReleased' or type(self.payload['cancelled']) is not bool):
            raise ValueError('cancelled must be a boolean on button release')
        if self.event_type not in ALLOWED_EVENTS:
            raise ValueError("event type is not allowlisted")
        if not self.session_id or self.sequence < 0 or self.state_version < 0:
            raise ValueError("event identity is invalid")
        if self.event_type.startswith("Button"):
            button = self.payload.get("button_id")
            if isinstance(button, bool) or not isinstance(button, int) or button not in range(1, 8):
                raise ValueError("button_id must be in range 1..7")


@dataclass
class _VoiceAdmission:
    key: tuple[str, str, int, str]
    consumed: bool = False


@dataclass
class DialogueMemory:
    status: str = "active"
    storage: str = "ram_only"
    last_valid_interaction_at: float | None = None
    expires_at: float | None = None
    ttl_seconds: int = RAM_TTL_SECONDS
    bounded_turns: list[str] = field(default_factory=list)
    story_checkpoint: dict[str, Any] | None = None

    def suspend(self, now: float) -> None:
        self.status, self.last_valid_interaction_at = "suspended", now
        self.expires_at = now + self.ttl_seconds

    def touch(self, now: float) -> None:
        if self.status == "expired":
            self.status, self.expires_at = "active", None
        self.last_valid_interaction_at = now
        if self.status == "suspended":
            self.expires_at = now + self.ttl_seconds

    def clear(self) -> None:
        self.status, self.bounded_turns, self.story_checkpoint = "expired", [], None
        self.last_valid_interaction_at = self.expires_at = None


@dataclass
class CanonicalState:
    session_id: str
    state_version: int = 0
    mode: str = "idle"
    substate: str = "idle"
    active_prompt_id: str | None = None
    playback_lease: str | None = None
    active_audio_id: str | None = None
    audio_status: str = "idle"
    audio_failure_code: str | None = None
    pending_tool: str | None = None
    pending_mode: str | None = None
    support_question_kind: str | None = None
    timeout_at: float | None = None
    history_tail: list[str] = field(default_factory=list)
    profile_ref: str | None = None
    profile_status: str = "anonymous"
    identity_request_id: str | None = None
    identity_ready_deadline: float | None = None
    interaction_idle: bool = False
    readiness_held_buttons: list[int] = field(default_factory=list)
    identity_expires_monotonic: float | None = None
    identity_capabilities: list[str] = field(default_factory=list)
    age_band: str = "3-5"
    age_band_source: str = "anonymous_default"
    display_name: str | None = None
    onboarding_name_collected: bool = False
    menu_tutorial_step: int = 0
    menu_tutorial_phase: str = "idle"
    menu_tutorial_audio_complete: bool = False
    menu_prompt_after_wave: bool = True
    menu_lit_buttons: list[int] = field(default_factory=list)
    menu_pressed_buttons: list[int] = field(default_factory=list)
    menu_repeat_attempts: int = 0
    story_menu_hint_seen: bool = False
    story_menu_hint_active: bool = False
    story_menu_skip_button: int | None = None
    selected_menu_button: int | None = None
    selected_menu_action: str | None = None
    grammatical_gender: str = "neutral"
    language: str = "ru-RU"
    timezone: str = "Europe/Moscow"
    age_years: int | None = None
    profile_data_incomplete: bool = False
    current_date: str | None = None
    age_policy: dict[str, Any] = field(default_factory=dict)
    scene_menu_seen: list[str] = field(default_factory=list)
    scene_menu_answers: dict[str, str] = field(default_factory=dict)
    scene_menu_pages: dict[str, int] = field(default_factory=dict)
    scene_menu_active: str | None = None
    scene_menu_phase: str = "idle"
    scene_menu_refresh_required: bool = False
    scene_menu_held: list[int] = field(default_factory=list)
    # Admitted button levels survive scene transitions; no invented press times.
    accepted_buttons_down: list[int] = field(default_factory=list)
    scene_menu_descriptor: dict[str, Any] = field(default_factory=dict)
    scene_menu_generation: int = field(default=0, repr=False)
    mood_result: str | None = None
    accessibility_policy: dict[str, Any] = field(default_factory=dict)
    schedule_ref: str | None = None
    handset: str = "down"
    # None is an explicit ended visit, even if the physical hook remains up.
    interaction_route: str | None = "handset"
    expected_inputs: list[str] = field(default_factory=list)
    welcome_t0_monotonic: float | None = None
    welcome_deadline_monotonic: float | None = None
    welcome_status: str = "idle"
    safety_severity: str = "none"
    active_story_id: str | None = None
    story_checkpoint_confirmed: bool = False
    public_story_checkpoint_pending: bool = False
    game_engine_state: GameState | None = None
    game_target: int | None = None
    game_level: int = 0
    active_game_id: str | None = None
    game_parameters: dict[str, Any] = field(default_factory=dict)
    game_progress: dict[str, int] = field(default_factory=dict)
    game_entry_seed: int = 0
    game_audio_stage: str = "idle"
    game_attempts: int = 0
    game_hint_stage: int = 0
    game_success_streak: int = 0
    pending_game_offer: str | None = None
    game_offer_id: str | None = None
    game_offer_ready: bool = False
    game_return_mode: str | None = None
    activity_return_mode: str | None = None
    dialogue_information_kind: str | None = None
    dialogue_memory: DialogueMemory = field(default_factory=DialogueMemory)
    seen_correlations: set[str] = field(default_factory=set, repr=False)
    last_sequence_by_source: dict[str, int] = field(default_factory=dict, repr=False)

    @property
    def interaction_open(self) -> bool:
        return (self.mode not in {"session_closed", "soft_end"}
                and (self.interaction_route == "panel" or (self.interaction_route == "handset" and self.handset == "up")))

    def snapshot(self) -> dict[str, Any]:
        value = asdict(self)
        value["interaction_active"] = self.interaction_open
        value.pop("seen_correlations", None)
        value.pop("last_sequence_by_source", None)
        for private_key in ("identity_request_id", "identity_capabilities", "identity_expires_monotonic", "identity_ready_deadline"):
            value.pop(private_key, None)
        # Profile display fields and schedule links stay inside the protected profile boundary.
        value.pop("display_name", None)
        value.pop("schedule_ref", None)
        value.pop("history_tail", None)
        value.pop("game_offer_id", None)
        value.pop("scene_menu_generation", None)
        # Dialogue turns may contain a child's name or spoken words. Keep
        # them in the private state object, never in public snapshots.
        value["dialogue_memory"] = {
            "status": self.dialogue_memory.status,
            "storage": self.dialogue_memory.storage,
            "expires_at": self.dialogue_memory.expires_at,
            "ttl_seconds": self.dialogue_memory.ttl_seconds,
            "story_checkpoint": self.dialogue_memory.story_checkpoint,
        }
        return value


def canonical_state_nodes() -> dict[str, dict[str, Any]]:
    """Minimum scenario metadata required by the product contract."""
    nodes = {
        name: {
            "state_version": 1,
            "allowed_events": sorted(ALLOWED_EVENTS), "expected_input": "contextual",
            "lighting_preset": "READY", "audio_policy": "application_owned",
            "timeout_policy": "explicit", "persistence_policy": "minimal",
            "success_transition": "explicit", "retry_transition": "explicit",
            "cancel_transition": "soft_end", "error_transition": "safety_stop",
        } for name in sorted(STATE_NAMES)
    }

    nodes["safety_stop"].update(
        expected_input="handset_down_or_reset", lighting_preset="SAFE_STOP",
        audio_policy="single_local_guidance", timeout_policy="none",
        persistence_policy="local_interaction_only", success_transition="idle",
        retry_transition="safety_stop", cancel_transition="idle",
    )
    return nodes


REQUIRED_EXHIBITION_PHRASES = (
    "PHRASE_001", "PHRASE_007", "PHRASE_008", "PHRASE_016",
    "PHRASE_009", "PHRASE_068", "PHRASE_074", "PHRASE_075", "PHRASE_078",
    "PHRASE_099", "PHRASE_102", "PHRASE_107", "PHRASE_108",
)

# Product requirements are kept separate from the currently recorded catalog;
# a nearby phrase must never accidentally satisfy P0-10.
REQUIRED_EXHIBITION_CONTENT = (
    {"requirement": "welcome", "phrase_id": "PHRASE_001", "transcript": "Привет, я Мишка!"},
    {"requirement": "introduction_request", "phrase_id": "PHRASE_078", "transcript": "Давай познакомимся!"},
    {"requirement": "name_question", "phrase_id": "PHRASE_099", "transcript": "Как тебя зовут?"},
    {"requirement": "age_question", "phrase_id": "PHRASE_108", "transcript": "Сколько тебе лет?"},
    {"requirement": "onboarding_closing", "phrase_id": "PHRASE_107", "transcript": "Рад знакомству!"},
    {"requirement": "onboarding_thanks", "phrase_id": "PHRASE_016", "transcript": "Здорово. Спасибо"},
    {"requirement": "menu_prompt", "phrase_id": "PHRASE_007", "transcript": "Что хочешь сделать?"},
    {"requirement": "menu_list", "phrase_id": "PHRASE_008", "transcript": "Один — игры. Два — сказки. Три — рассказать или спросить. Четыре — новая сказка. Пять — помощь. Шесть — поговорить. Семь — день и планы"},
    {"requirement": "menu_repeat_prompt", "phrase_id": "PHRASE_009", "transcript": "Повторить меню?"},
    {"requirement": "listen", "phrase_id": "PHRASE_068", "transcript": "Я слушаю"},
    {"requirement": "retry", "phrase_id": "PHRASE_074", "transcript": "Я не расслышал. Скажи ещё раз"},
    {"requirement": "age_button_hint", "phrase_id": "PHRASE_075", "transcript": "Можно показать кнопкой"},
)


class VoiceManifest:
    """Exact-match resolver with fail-closed WAV validation.

    This is the only runtime phrase catalog. Discovery of a file never
    implies approval, and a malformed asset never reaches a playback adapter.
    """
    ALLOWED_STATUSES = frozenset({"missing", "placeholder", "recorded_pending_approval", "approved", "invalid"})
    ALLOWED_SAMPLE_RATES = frozenset({22050, 24000, 48000})
    ALLOWED_FORMATS = frozenset({"wav_pcm_s16le"})
    CATALOG_VERSION = "mishka.voice-manifest/v1"
    VALIDATOR_VERSION = "gr07/v1"
    ALLOWED_FORMATS = frozenset({"wav_pcm_s16le"})
    CATALOG_VERSION = "mishka.voice-manifest/v1"
    VALIDATOR_VERSION = "gr07/v1"
    def __init__(self, path: str | Path, *, allow_development_assets: bool = False) -> None:
        self.path = Path(path)
        self.allow_development_assets = bool(allow_development_assets)
        self.document = self._load()
        self._validated_cache: list[dict[str, Any]] | None = None
        self._validation_report_cache: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"required_recorded_count": 106, "assets": [], "status": "missing"}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"required_recorded_count": 106, "assets": [], "status": "invalid"}
        return value if isinstance(value, dict) else {"required_recorded_count": 106, "assets": [], "status": "invalid"}

    @classmethod
    def inspect_wav(cls, path: Path, *, allow_silence: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {"validation_status": "missing", "validation_reason": "file_missing"}
        try:
            with wave.open(str(path), "rb") as source:
                channels, width, rate, frames = (source.getnchannels(), source.getsampwidth(),
                                                  source.getframerate(), source.getnframes())
                compression, raw = source.getcomptype(), source.readframes(frames)
            result.update({"format": "wav_pcm_s16le", "sample_rate": rate, "channels": channels,
                           "sample_width_bits": width * 8, "frame_count": frames,
                           "duration_ms": round(frames * 1000 / rate) if rate else None})
            if compression != "NONE": raise ValueError("compressed_wav")
            if channels != 1: raise ValueError("channels_must_be_mono")
            if width != 2: raise ValueError("sample_width_must_be_16_bit")
            if rate not in cls.ALLOWED_SAMPLE_RATES: raise ValueError("unsupported_sample_rate")
            if frames <= 0 or not raw or len(raw) != frames * channels * width:
                raise ValueError("empty_or_truncated_payload")
            samples = struct.unpack("<%dh" % frames, raw)
            peak = max(abs(value) for value in samples)
            rms = math.sqrt(sum(value * value for value in samples) / frames)
            clipped = sum(1 for value in samples if abs(value) >= 32767)
            result.update({"rms": round(rms, 2), "peak_abs": peak, "clipped_sample_count": clipped})
            if not allow_silence and peak == 0: raise ValueError("silent_audio")
            if clipped / frames > 0.01: raise ValueError("excessive_clipping")
            result.update({"validation_status": "valid", "validation_reason": None})
        except (OSError, EOFError, struct.error, wave.Error, ValueError) as exc:
            result.setdefault("format", "wav_pcm_s16le")
            result["validation_status"] = "invalid"
            result["validation_reason"] = str(exc)[:80]
        return result

    @staticmethod
    def _safe_relative_path(value: Any) -> bool:
        if not isinstance(value, str) or not value.strip():
            return False
        path = Path(value)
        return not path.is_absolute() and ".." not in path.parts

    def _validate_asset(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        relative, expected = raw.get("asset_path"), raw.get("asset_sha256")
        if not self._safe_relative_path(relative) or not isinstance(expected, str):
            return None
        path = self.path.parent / relative
        try:
            data = path.read_bytes()
        except OSError:
            return None
        if hashlib.sha256(data).hexdigest() != expected:
            return None
        inspected = self.inspect_wav(path, allow_silence=raw.get("status") == "placeholder")
        if inspected.get("validation_status") != "valid":
            return None
        return dict(raw)

    def _all_validated_assets(self) -> list[dict[str, Any]]:
        # The manifest and its hashes are immutable for a process lifetime.
        # Cache the expensive full WAV inspection so /ready and WebPanel
        # refreshes cannot repeatedly decode the recorded files.
        if self._validated_cache is not None:
            return [dict(item) for item in self._validated_cache]
        result: list[dict[str, Any]] = []
        for raw in self.document.get("assets", []):
            if isinstance(raw, dict) and raw.get("status") in self.ALLOWED_STATUSES:
                validated = self._validate_asset(raw)
                if validated is not None:
                    result.append(validated)
        self._validated_cache = [dict(item) for item in result]
        return [dict(item) for item in result]

    def _approved_assets(self, *, include_development: bool = False) -> list[dict[str, Any]]:
        allowed = {"approved"}
        if include_development:
            allowed |= {"recorded_pending_approval", "placeholder"}
        return [asset for asset in self._all_validated_assets() if asset.get("status") in allowed]

    def validation_report(self) -> dict[str, Any]:
        """Return deterministic content diagnostics for simulator/release review."""
        if self._validation_report_cache is not None:
            return dict(self._validation_report_cache)
        raw = [item for item in self.document.get("assets", []) if isinstance(item, dict)]
        errors, warnings, blockers = [], [], []
        ids = [str(item.get("phrase_id") or "") for item in raw]
        duplicate_ids = sorted({item for item in ids if ids.count(item) > 1})
        refs = [str(item.get("asset_path") or "") for item in raw]
        duplicate_refs = sorted({item for item in refs if refs.count(item) > 1})
        if duplicate_ids:
            errors.append({"code": "duplicate_phrase_id", "items": duplicate_ids})
        if duplicate_refs:
            errors.append({"code": "duplicate_asset_ref", "items": duplicate_refs})
        valid_ids, pending, invalid, missing = set(), [], [], []
        referenced = set()
        for index, item in enumerate(raw):
            phrase_id = str(item.get("phrase_id") or "")
            prefix = {"index": index, "phrase_id": phrase_id or None}
            required = ("phrase_id", "transcript", "language", "asset_path", "asset_sha256", "status", "format")
            absent = [field for field in required if not item.get(field)]
            if absent:
                errors.append({**prefix, "code": "missing_required_field", "fields": absent})
                invalid.append(phrase_id)
                continue
            status = str(item.get("status"))
            if status not in self.ALLOWED_STATUSES:
                errors.append({**prefix, "code": "unsupported_status", "status": status})
                invalid.append(phrase_id)
                continue
            path_ref = item.get("asset_path")
            if not self._safe_relative_path(path_ref):
                errors.append({**prefix, "code": "unsafe_asset_path", "asset_ref": path_ref})
                invalid.append(phrase_id)
                continue
            asset_path = self.path.parent / str(path_ref)
            if asset_path.exists():
                referenced.add(asset_path.resolve())
            if status == "missing":
                errors.append({**prefix, "code": "missing_asset", "asset_ref": path_ref})
                missing.append(phrase_id)
                continue
            if status == "invalid":
                errors.append({**prefix, "code": "declared_invalid_asset", "asset_ref": path_ref})
                invalid.append(phrase_id)
                continue
            if not asset_path.is_file():
                errors.append({**prefix, "code": "missing_asset", "asset_ref": path_ref})
                missing.append(phrase_id)
                continue
            try:
                data = asset_path.read_bytes()
            except OSError as exc:
                errors.append({**prefix, "code": "asset_unreadable", "detail": str(exc)[:120]})
                invalid.append(phrase_id)
                continue
            if hashlib.sha256(data).hexdigest() != str(item.get("asset_sha256")):
                errors.append({**prefix, "code": "hash_mismatch", "asset_ref": path_ref})
                invalid.append(phrase_id)
                continue
            inspected = self.inspect_wav(asset_path, allow_silence=status == "placeholder")
            if inspected.get("validation_status") != "valid":
                errors.append({**prefix, "code": "invalid_wav", "asset_ref": path_ref,
                               "reason": inspected.get("validation_reason")})
                invalid.append(phrase_id)
                continue
            if str(item.get("format")) not in self.ALLOWED_FORMATS:
                errors.append({**prefix, "code": "unsupported_format", "format": item.get("format")})
                invalid.append(phrase_id)
                continue
            mismatches = [field for field in ("sample_rate", "channels", "sample_width_bits", "frame_count", "duration_ms")
                          if field in item and item.get(field) != inspected.get(field)]
            if mismatches:
                errors.append({**prefix, "code": "metadata_mismatch", "fields": mismatches})
                invalid.append(phrase_id)
                continue
            valid_ids.add(phrase_id)
            if status == "recorded_pending_approval":
                pending.append(phrase_id)
        source_ref = self.document.get("source_catalog")
        source_path = self.path.parent / str(source_ref) if self._safe_relative_path(source_ref) else None
        if source_path is None or not source_path.is_file():
            errors.append({"code": "missing_source_catalog", "source_catalog": source_ref})
        else:
            try:
                catalog = load_phrase_catalog(source_path)
                transcripts = {str(item.get("phrase_id")): str(item.get("transcript")) for item in raw}
                if catalog != transcripts:
                    errors.append({"code": "source_catalog_divergence", "source_catalog": source_ref})
            except (OSError, UnicodeError, ValueError) as exc:
                errors.append({"code": "source_catalog_unreadable", "detail": str(exc)[:120]})
        audio_root = self.path.parent / "voice"
        orphan = sorted(str(asset.relative_to(self.path.parent)) for asset in audio_root.rglob("*.wav")
                        if asset.is_file() and asset.resolve() not in referenced)
        if orphan:
            warnings.append({"code": "orphan_audio_asset", "items": orphan})
        if pending:
            warnings.append({"code": "pending_approval", "items": sorted(set(pending)), "count": len(set(pending))})
        required = tuple(str(value) for value in self.document.get("required_phrase_ids", REQUIRED_EXHIBITION_PHRASES))
        required_content = []
        for phrase_id in required:
            item = next((entry for entry in raw if str(entry.get("phrase_id")) == phrase_id), None)
            status = str(item.get("status")) if item else "missing"
            ready = phrase_id in valid_ids
            required_content.append({"phrase_id": phrase_id, "status": status, "valid": ready})
            if not ready:
                blockers.append({"code": "required_exhibition_asset_invalid_or_missing", "phrase_id": phrase_id})
            elif status != "approved":
                blockers.append({"code": "required_exhibition_asset_pending_approval", "phrase_id": phrase_id, "status": status})
        required_contract = []
        for requirement in REQUIRED_EXHIBITION_CONTENT:
            phrase_id = requirement.get("phrase_id")
            item = next((entry for entry in raw if phrase_id and str(entry.get("phrase_id")) == phrase_id), None)
            actual = str(item.get("transcript")) if item else None
            exact = bool(item and actual == requirement["transcript"] and phrase_id in valid_ids)
            row = {**requirement, "status": str(item.get("status")) if item else "missing",
                   "asset_exists": bool(item and (self.path.parent / str(item.get("asset_path") or "")).is_file()),
                   "approved": bool(exact and item.get("status") == "approved"), "exact": exact}
            required_contract.append(row)
            if phrase_id is None or not exact:
                blockers.append({"code": "required_exact_transcript_missing", "requirement": requirement["requirement"],
                                 "phrase_id": phrase_id, "expected_transcript": requirement["transcript"],
                                 "actual_transcript": actual})
            elif item.get("status") != "approved":
                blockers.append({"code": "required_exact_transcript_pending_approval", "phrase_id": phrase_id,
                                 "status": item.get("status")})
        approved = sum(1 for item in raw if str(item.get("status")) == "approved" and str(item.get("phrase_id")) in valid_ids)
        required_approved = int(self.document.get("required_approved_count", self.document.get("required_recorded_count", 106)))
        if approved < required_approved:
            blockers.append({"code": "approved_audio_below_required", "approved_count": approved, "required_count": required_approved})
        result = {
            "catalog_version": str(self.document.get("catalog_version") or self.document.get("contract") or self.CATALOG_VERSION),
            "validator_version": self.VALIDATOR_VERSION,
            "errors": errors, "warnings": warnings, "production_blockers": blockers,
            "valid_entries": len(valid_ids), "invalid_entries": sorted(set(invalid)),
            "missing_assets": sorted(set(missing)), "approved_count": approved,
            "pending_approval": sorted(set(pending)), "required_exhibition_content": required_content,
            "required_exhibition_contract": required_contract,
            "duplicate_asset_refs": duplicate_refs, "orphan_audio_assets": orphan,
            "summary": {"error_count": len(errors), "warning_count": len(warnings),
                        "production_blocker_count": len(blockers)},
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        }

        self._validation_report_cache = dict(result)
        return result

    def diagnose(self, phrase_id: str, content_hash: str = "") -> dict[str, Any]:
        item = next((entry for entry in self.document.get("assets", [])
                     if isinstance(entry, dict) and str(entry.get("phrase_id")) == str(phrase_id)), None)
        if item is None:
            return {"phrase_id": str(phrase_id), "status": "missing", "code": "content_id_missing"}
        if content_hash and str(item.get("content_hash")) != str(content_hash):
            return {"phrase_id": str(phrase_id), "status": "invalid", "code": "content_hash_mismatch"}
        valid = any(str(entry.get("phrase_id")) == str(phrase_id) for entry in self._all_validated_assets())
        status = str(item.get("status") or "invalid")
        if not valid:
            code = "asset_invalid_or_missing"
        elif status == "approved" or (self.allow_development_assets and status in {"recorded_pending_approval", "placeholder"}):
            code = "ready"
        else:
            code = "approval_required"
        return {"phrase_id": str(phrase_id), "status": status, "code": code, "asset_ref": item.get("asset_path")}

    def readiness(self) -> dict[str, Any]:
        raw_assets = [item for item in self.document.get("assets", []) if isinstance(item, dict)]
        ids = [str(item.get("phrase_id")) for item in raw_assets]
        duplicates = sorted({item for item in ids if ids.count(item) > 1})
        valid = self._all_validated_assets()
        by_id = {str(item.get("phrase_id")): item for item in valid}
        required = int(self.document.get("required_approved_count", self.document.get("required_recorded_count", 106)))
        approved = [item for item in valid if item.get("status") == "approved"]
        active = self._approved_assets(include_development=self.allow_development_assets)
        status_counts = {status: sum(item.get("status") == status for item in raw_assets) for status in self.ALLOWED_STATUSES}
        missing_or_invalid = required - len(by_id)
        report = {"required_recorded_count": int(self.document.get("required_recorded_count", 106)),
                  "catalog_count": len(raw_assets), "duplicate_phrase_ids": duplicates,
                  "approved_recorded_count": len(approved), "development_available_count": len(active),
                  "valid_count": len(valid), "missing_or_invalid_count": max(0, missing_or_invalid),
                  "status_counts": status_counts,
                  "ready": (not duplicates and len(active) >= required) if self.allow_development_assets else (not duplicates and len(approved) >= required),
                  "status": self.document.get("status", "unknown")}
        report.update(self.validation_report())
        report["catalog_technically_valid"] = not report["errors"]
        report["simulator_usable"] = report["catalog_technically_valid"] and bool(active)
        report["approved_catalog_ready"] = report["catalog_technically_valid"] and len(approved) >= required
        # WAV validation cannot prove speaker/output acceptance.
        report["physical_audio_verified"] = False
        report["physical_production_ready"] = bool(
            report["catalog_technically_valid"] and report["approved_catalog_ready"]
            and report["physical_audio_verified"]
        )
        return report

    def resolve(self, phrase_id: str, content_hash: str = "") -> dict[str, Any] | None:
        for asset in self._approved_assets(include_development=self.allow_development_assets):
            if asset.get("phrase_id") == phrase_id and (not content_hash or asset.get("content_hash") == content_hash):
                return asset
        return None


class FixedStoryManifest:
    REQUIRED_TITLES = ("Айболит", "Про Белочку", "У Лукоморья", "Кролик по имени Мягкие Лапки и про Звезду", "Про Незнайку")

    def __init__(self, path: str | Path, root: str | Path) -> None:
        self.path, self.root = Path(path), Path(root)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        except (OSError, json.JSONDecodeError):
            data = {}
        self.document = data if isinstance(data, dict) else {}
        self.stories = self.document.get("stories", {})

    def get_verified(self, story_id: str) -> dict[str, Any] | None:
        item = self.stories.get(story_id)
        if not isinstance(item, dict) or item.get("rights_status") not in {"licensed", "public_domain", "owner_authored", "operator_approved"}:
            return None
        audio_filename, audio_expected = item.get("audio_file"), item.get("audio_sha256")
        if not isinstance(audio_filename, str) or not isinstance(audio_expected, str):
            return None
        try:
            audio_actual = hashlib.sha256((self.root / audio_filename).read_bytes()).hexdigest()
        except OSError:
            return None
        if audio_actual != audio_expected:
            return None
        # Exact text is optional for operator-provided recorded stories.
        # If present, it remains fail-closed and verified.
        text_filename, text_expected = item.get("exact_text_file"), item.get("text_sha256")
        if text_filename is not None or text_expected is not None:
            if not isinstance(text_filename, str) or not isinstance(text_expected, str):
                return None
            try:
                text_actual = hashlib.sha256((self.root / text_filename).read_bytes()).hexdigest()
            except OSError:
                return None
            if text_actual != text_expected:
                return None
        try:
            with wave.open(str(self.root / audio_filename), "rb") as source:
                rate = source.getframerate()
                if rate <= 0:
                    return None
                duration_ms = round(source.getnframes() * 1000 / rate)
        except (OSError, wave.Error):
            return None
        return {**item, "duration_ms": duration_ms}

    def get_development_placeholder(self) -> dict[str, Any] | None:
        item = self.document.get("development_placeholder")
        if not isinstance(item, dict) or item.get("development_only") is not True:
            return None
        filename, expected = item.get("audio_file"), item.get("audio_sha256")
        if not isinstance(filename, str) or not isinstance(expected, str):
            return None
        try:
            actual = hashlib.sha256((self.root / filename).read_bytes()).hexdigest()
        except OSError:
            return None
        if actual != expected:
            return None
        return dict(item)

def validate_generated_story(candidate: Mapping[str, Any], brief: Mapping[str, Any]) -> tuple[bool, str | None]:
    required = {"title", "text", "age_band", "estimated_duration_sec", "safety_tags", "ending_state"}
    if set(candidate) < required or candidate.get("age_band") != brief.get("age_band"):
        return False, "schema_or_age_band"
    title = candidate.get("title")
    text = candidate.get("text")
    tags = candidate.get("safety_tags")
    if not isinstance(title, str) or not title.strip() or not isinstance(text, str) or not text.strip():
        return False, "schema_or_empty_text"
    if not isinstance(tags, list) or any(not isinstance(item, str) for item in tags):
        return False, "schema_safety_tags"
    if len(text) > 6000 or STORY_FORBIDDEN.search(title) or STORY_FORBIDDEN.search(text) or PERSONA_FORBIDDEN.search(title) or PERSONA_FORBIDDEN.search(text):
        return False, "forbidden_content"
    theme = str(brief.get("theme") or brief.get("theme_id") or "")
    if theme and theme.casefold() not in {value.casefold() for value in ALLOWED_STORY_THEMES}:
        return False, "unsupported_theme"
    constraints = {str(item) for item in brief.get("content_constraints", ())}
    if "avoid_parent_assumptions" in constraints and re.search(r"\b(мама|папа|родител\w*)\b", text, re.I):
        return False, "trigger_parent_assumption"
    if "avoid_body_assumptions" in constraints and re.search(r"\b(встань|побег|возьми двумя руками)\w*", text, re.I):
        return False, "trigger_body_assumption"
    if candidate.get("ending_state") != "safe_resolved":
        return False, "unsafe_ending"
    if any(str(value).casefold() in text.casefold() for value in brief.get("forbidden_topics", ())):
        return False, "forbidden_topic"
    name = brief.get("display_name")
    if name and text.casefold().count(str(name).casefold()) > 2:
        return False, "name_overuse"
    duration = candidate.get("estimated_duration_sec")
    bounds = {"1-3": (45, 70), "3-5": (60, 100), "5-7": (90, 150)}.get({"4-5":"3-5","6-7":"5-7"}.get(str(brief.get("age_band")),str(brief.get("age_band"))))
    if isinstance(duration, bool) or not isinstance(duration, int) or not bounds or not bounds[0] <= duration <= bounds[1]:
        return False, "duration"
    return True, None


def _strip_explicit_name_reply_prefix(text: str) -> str:
    # A correction such as “Нет, меня зовут Вася” is not a refusal. Only
    # discard a discourse prefix before an explicit naming cue, never infer
    # a name from an arbitrary trailing clause.
    return re.sub(
        r"^(?:нет|да|спасибо)(?:\s*[,;:—–-]\s*|\s+)"
        r"(?=(?:меня\s+зовут|мо[её]\s+имя|зови\s+меня|это\s+я)\s+)",
        "", text, count=1, flags=re.IGNORECASE,
    )


def onboarding_declined(value: str) -> bool:
    """Recognize an explicit short refusal before extracting any name slot.

    Match whole words at the first meaningful clause, not substrings in a
    genuine name and not an unconstrained keyword anywhere in an utterance.
    Safety/adult-request routing is performed before this helper is used.
    """
    text = " ".join(str(value or "").casefold().strip().split()).strip(" .!?…")
    if not text or len(text) > 160:
        return False
    text = _strip_explicit_name_reply_prefix(text)
    clauses = re.split(r"[,.;!?…:—–]", text)
    first = clauses[0].strip()
    if first == "спасибо" and len(clauses) > 1:
        first = clauses[1].strip()
    if re.match(r"^(?:нет|неа)\b", first):
        return True
    if re.match(r"^(?:я\s+)?не\s+(?:хочу|буду|скажу|знаю|помню)\b", first):
        return True
    if re.match(r"^(?:сво[её]\s+)?(?:имя|возраст)\s+(?:я\s+)?не\s+(?:скажу|назову)\b", first):
        return True
    return bool(re.fullmatch(
        r"(?:(?:я )?(?:давай|можно|хочу)(?: (?:лучше|сразу))? (?:играть|поиграть)"
        r"|пропусти(?:м)?(?: пожалуйста)?|неважно)", first))


def normalize_exhibition_name(value: str) -> str | None:
    """Extract a spoken name from a short answer.

    STT returns the whole utterance, not just the semantic slot.  In
    particular, ``меня зовут Вася`` must produce ``Вася`` rather than the
    first token (``меня``).  Keep only a bounded name-shaped prefix and never
    retain the transcript itself in session state.
    """
    text = " ".join(str(value or "").strip().split())
    if not text or len(text) > 160 or onboarding_declined(text):
        return None
    text = _strip_explicit_name_reply_prefix(text)
    # Stop at normal sentence punctuation so a natural answer such as
    # ``меня зовут Вася, мне семь`` cannot absorb the second clause.
    text = re.split(r"[,.;!?…]", text, maxsplit=1)[0].strip(" -—:;")
    cue = re.match(
        r"^(?:меня\s+зовут|мо[её]\s+имя|зови\s+меня|это\s+я|я)\s+(.+)$",
        text,
        flags=re.IGNORECASE,
    )
    candidate = cue.group(1).strip() if cue else text
    stopwords = {
        "меня", "зовут", "моё", "мое", "имя", "я", "это", "и", "мне",
        "лет", "года", "год", "летний", "летняя",
    }
    parts = re.findall(r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё-]*", candidate)
    if not parts:
        return None
    selected: list[str] = []
    for part in parts[:3]:
        if part.casefold() in stopwords:
            break
        if len(part) < 2 or len(part) > 32:
            break
        selected.append(part)
    if not selected:
        return None
    name = " ".join(selected)
    if not re.fullmatch(r"[^\W\d_]+(?:[ '-][^\W\d_]+){0,2}", name, flags=re.UNICODE):
        return None
    return name[:1].upper() + name[1:]


_EXHIBITION_AGE_WORDS = {
    "один": 1, "одна": 1, "раз": 1,
    "два": 2, "две": 2,
    "три": 3, "четыре": 4, "пять": 5, "шесть": 6, "семь": 7,
}


def normalize_exhibition_age(value: str) -> int | None:
    """Accept one direct age answer; reject contradictions and unrelated numbers."""
    text = " ".join(str(value or "").casefold().strip().split()).strip(" .!?…")
    if not text or len(text) > 120 or onboarding_declined(text):
        return None
    word = r"(?:[1-7]|один|одна|два|две|три|четыре|пять|шесть|семь)"
    match = re.fullmatch(rf"(?:(?:мне|мне уже) )?({word})(?: (?:год|года|лет))?", text)
    if not match:
        return None
    value = match.group(1)
    return int(value) if value.isdecimal() else _EXHIBITION_AGE_WORDS[value]


def normalize_exhibition_repeat_answer(value: str) -> str | None:
    """Reduce a natural yes/no answer to the repeat-menu decision."""
    text = " ".join(str(value or "").casefold().strip().split())
    if not text:
        return None
    if re.search(r"\b(нет|неа|не хочу|не надо|не нужно|не повторяй|достаточно|понятно)\b", text):
        return "no"
    if re.search(r"\b(да|ага|угу|конечно|хочу|повтори|повторить|ещё раз|еще раз)\b", text):
        return "yes"
    return None


from .scene_menus import SceneMenuMixin

class CanonicalRuntime(SceneMenuMixin):
    """In-memory state machine with stale-event, effect and RAM-TTL guarantees."""
    def __init__(self, *, now: Callable[[], float] = _utc_now, monotonic_now: Callable[[], float] = time.monotonic, stories: FixedStoryManifest | None = None, allow_development_placeholders: bool = False, exhibition_mode: bool = False, optional_identity: bool = False) -> None:
        self.now, self.monotonic_now, self.stories, self.sessions = now, monotonic_now, stories, {}
        self.allow_development_placeholders = bool(allow_development_placeholders)
        self.exhibition_mode = bool(exhibition_mode)
        self.optional_identity = bool(optional_identity)
    @staticmethod
    def _story_id_for_button(button: int) -> str | None:
        return {
            1: "fixed_aibolit_v1", 2: "fixed_squirrel_v1", 3: "fixed_lukomorye_v1",
            4: "fixed_soft_paws_star_v1", 5: "fixed_neznayka_v1",
        }.get(button)

    @staticmethod
    def _exhibition_age_band(age_years: int) -> str:
        return "1-3" if age_years <= 3 else "4-5" if age_years <= 5 else "6-7"

    def _select_exhibition_age(self, state: CanonicalState, age_years: int, *, source: str, now: float) -> dict[str, Any]:
        band = self._exhibition_age_band(age_years)
        policy = policy_for_band(band, source=source)
        if not (state.profile_status == "identified" and state.age_band_source in {"profile_dob", "data14_age_years"}):
            state.age_band, state.age_band_source = band, source
            state.age_policy, state.age_years = policy.as_dict(), age_years
        state.dialogue_memory.bounded_turns.append({"kind": "age_selection", "age": age_years, "age_band": band, "source": source})
        state.dialogue_memory.touch(now)
        state.mode, state.substate = "exhibition_age_complete", "awaiting_closing_audio"
        state.expected_inputs = ["audio_completed", "handset_down"]
        state.state_version += 1
        return self._decision(state, [
            {"type": "speak", "phrase_id": "PHRASE_107"},
            {"type": "lighting_select_button", "button": age_years, "state": 1},
            {"type": "exhibition_onboarding_step", "step": "age_selected", "age": age_years, "age_band": band, "source": source},
        ], "exhibition_age_selected", clear_audio=True)


    def reset(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)

    def _new_state(self, session_id: str, profile: Mapping[str, Any] | None) -> CanonicalState:
        band, source = age_band_from_profile(profile)
        value = profile or {}
        profile_now = datetime.now(ZoneInfo(str(value.get("timezone") or "Europe/Moscow")))
        details = age_details(profile, now=profile_now)
        gender = str(value.get("grammatical_gender") or "neutral")
        if gender not in ALLOWED_GENDERS:
            gender = "neutral"
        name = value.get("display_name")
        if not isinstance(name, str) or not name.strip():
            name = None
        progress = value.get("progress") if isinstance(value.get("progress"), Mapping) else {}
        mastery = progress.get("game_mastery") if isinstance(progress.get("game_mastery"), Mapping) else {}
        prior_level = max((int(item) for item in mastery.values() if isinstance(item, int) and not isinstance(item, bool)), default=0)
        profile_ref = _opaque_profile_ref(profile)
        identified = bool(profile_ref)
        return CanonicalState(session_id=session_id, profile_ref=profile_ref,
                              profile_status="identified" if identified else "anonymous",
                              age_band=band, age_band_source=source,
                              language=str(value.get("language") or "ru-RU"),
                              timezone=str(value.get("timezone") or "Europe/Moscow"),
                              age_years=details.get("age_years"),
                              profile_data_incomplete=bool(details.get("profile_data_incomplete")),
                              current_date=details.get("current_date"),
                              age_policy=policy_for(profile, now=profile_now).as_dict(),
                              display_name=name[:80] if name else None,
                              grammatical_gender=gender,
                              accessibility_policy=accessibility_policy_from_profile(profile),
                              schedule_ref=str(value.get("schedule_ref")) if value.get("schedule_ref") else None,
                              game_level=max(0, min(9, prior_level)))

    def clear_identity(self, state: CanonicalState) -> None:
        """Scrub private and derived identity independently of local safety mode."""
        state.activity_return_mode = state.dialogue_information_kind = None
        state.interaction_idle = False
        fresh = self._new_state(state.session_id, None)
        for name in (
            "profile_ref", "profile_status", "display_name", "grammatical_gender",
            "language", "timezone", "age_band", "age_band_source", "age_years",
            "age_policy", "profile_data_incomplete", "current_date",
            "accessibility_policy", "schedule_ref", "onboarding_name_collected",
        ):
            setattr(state, name, getattr(fresh, name))
        state.story_menu_hint_seen = state.story_menu_hint_active = False
        state.story_menu_skip_button = None
        state.scene_menu_seen.clear(); state.scene_menu_answers.clear(); state.scene_menu_pages.clear()
        state.scene_menu_active=None; state.scene_menu_phase="idle"; state.scene_menu_refresh_required=False; state.scene_menu_held.clear()
        state.scene_menu_descriptor={}
        state.history_tail.clear()
        state.dialogue_memory.clear()
        state.pending_tool = None
        state.pending_mode = None
        state.active_story_id = state.active_game_id = None
        state.story_checkpoint_confirmed = state.public_story_checkpoint_pending = False
        state.game_level = state.game_attempts = state.game_hint_stage = state.game_success_streak = 0
        state.game_parameters = {}
        state.game_progress.clear()
        state.game_entry_seed = 0
        state.game_audio_stage = "idle"
        state.mood_result = None
        state.identity_request_id = None
        state.identity_ready_deadline = None
        state.identity_expires_monotonic = None
        state.identity_capabilities = []

    @staticmethod
    def has_private_context(state: CanonicalState) -> bool:
        return bool(state.profile_ref or state.display_name or state.onboarding_name_collected
                    or state.history_tail or state.dialogue_memory.bounded_turns
                    or state.dialogue_memory.story_checkpoint or state.active_story_id
                    or state.active_game_id or state.pending_game_offer or state.game_progress)

    def _start_exhibition_welcome(self, state, monotonic_now, *, reason, preserve_lighting=False):
        state.mode = "voice_welcome"
        state.expected_inputs = ["handset_down"]
        state.welcome_t0_monotonic = monotonic_now
        state.welcome_deadline_monotonic = monotonic_now + EXHIBITION_WELCOME_DELAY_SECONDS
        state.welcome_status = "pending"
        return self._decision(state, [
            {"type": "lighting_wave_intent", "pattern": "handset_lift_wave", "source": "canonical", **({"preserve_current": True} if preserve_lighting else {})},
            {"type": "play_asset", "asset_id": "assets/sounds/ringtone.wav", "content_id": "RINGTONE"},
            {"type": "welcome_timer_scheduled", "phrase_id": "PHRASE_001",
             "delay_seconds": EXHIBITION_WELCOME_DELAY_SECONDS,
             "t0_monotonic": state.welcome_t0_monotonic,
             "deadline_monotonic": state.welcome_deadline_monotonic},
        ], reason)

    def _enter_local_safety(self, state: CanonicalState, *, severity: str,
                            reason: str, guidance: bool = True) -> dict[str, Any]:
        state.mode = "safety_stop"
        state.safety_severity = severity
        state.expected_inputs = ["handset_down", "reset"]
        state.pending_tool = state.pending_mode = state.support_question_kind = None
        state.active_story_id = state.active_game_id = None
        state.welcome_status = "cancelled"
        state.welcome_t0_monotonic = state.welcome_deadline_monotonic = None
        state.dialogue_memory.clear()
        state.state_version += 1
        effects = [{"type": "audio_stop"}, {"type": "capture_close"},
                   {"type": "lighting_preset", "preset": "SAFE_STOP"}]
        if guidance:
            effects.append({"type": "speak", "phrase_id": "PHRASE_091"})
        return self._decision(state, effects, reason, clear_audio=True)

    def _finish_local_safety(self, state: CanonicalState, event: CanonicalEvent,
                             *, panel_end: bool = False) -> dict[str, Any]:
        prior_deadline = state.welcome_deadline_monotonic
        fresh = self._new_state(state.session_id, None)
        fresh.handset = "down" if event.event_type == "HandsetPlaced" else state.handset
        fresh.interaction_route = None
        fresh.mode = "session_closed" if panel_end or fresh.handset == "up" else "idle"
        fresh.expected_inputs = ["handset_up"] if fresh.handset == "down" else ["handset_down"]
        fresh.state_version = state.state_version + 1
        fresh.last_sequence_by_source = dict(state.last_sequence_by_source)
        fresh.seen_correlations = set(state.seen_correlations)
        self.sessions[state.session_id] = fresh
        return self._decision(fresh, [
            {"type": "audio_stop"}, {"type": "capture_close"},
            {"type": "welcome_timer_cancelled", "deadline_monotonic": prior_deadline},
            {"type": "exhibition_effects_cancelled"}, {"type": "identity_cleared"},
            {"type": "lighting_dark"},
        ], "soft_end" if panel_end else "local_safety_closed",
            reset_session=not panel_end, clear_audio=True)

    def _support_no(self, state):
        if state.support_question_kind != "fear":
            state.support_question_kind = "fear"
            state.state_version += 1
            return self._decision(state, [{"type": "audio_stop"},
                {"type": "lighting_preset", "preset": "HELP"},
                {"type": "support_question", "question": "fear"}], "support_continue", clear_audio=True)
        state.support_question_kind = None
        state.mode = "root_menu"
        state.expected_inputs = [f"button_{i}" for i in range(1, 8)] + ["handset_down"]
        state.state_version += 1
        return self._decision(state, [{"type": "audio_stop"},
            {"type": "lighting_preset", "preset": "ROOT_MENU"},
            {"type": "speak", "phrase_id": "PHRASE_087"}], "support_declined", clear_audio=True)

    def _personal_welcome_menu(self, state, reason):
        state.mode = "exhibition_menu_tutorial_prompt"
        state.expected_inputs = ["audio_completed", "handset_down"]
        state.menu_prompt_after_wave = True
        state.state_version += 1
        return self._decision(state, [{"type": "capture_close"}, {"type": "audio_stop"},
            {"type": "speak", "phrase_id": "PHRASE_007"}], reason, clear_audio=True)

    @staticmethod
    def _game_lighting(engine: GameState | None) -> dict[str, Any]:
        challenge = current_challenge(engine) if engine else None
        return {"type": "lighting_preset", "preset": "GAME_OUTPUT",
                "buttons": list(desired_buttons(engine)) if engine else [],
                "display":getattr(challenge, 'display', 'digits') if challenge else 'colors'}

    def _game_exit(self, state: CanonicalState, reason: str, *, preserve_output: bool = False) -> dict[str, Any]:
        engine = state.game_engine_state
        if engine is not None:
            state.game_progress[engine.game_id] = max(
                state.game_progress.get(engine.game_id, 0), getattr(engine, 'level', 0))
        returning = (state.game_return_mode == "bear_dialogue" or state.activity_return_mode == "bear_dialogue") and state.interaction_open
        return_to_game_menu = state.game_return_mode == "game_menu" and not returning
        target_mode = "bear_dialogue" if returning else "game_menu" if return_to_game_menu else "root_menu"
        state.mode, state.active_game_id = target_mode, None
        state.game_return_mode = None
        state.activity_return_mode = None
        state.game_audio_stage = "idle"
        state.game_target = None
        state.game_parameters = {}
        state.selected_menu_button = None
        state.scene_menu_held = []
        state.expected_inputs = (["speech", "button_7", "handset_down"] if returning else
                                 [f"button_{i}" for i in range(1, 8)] + ["handset_down"])
        state.state_version += 1
        preset = "BEAR_DIALOGUE" if returning else "GAME_MENU" if return_to_game_menu else "ROOT_MENU"
        effects = ([] if preserve_output else [{"type":"audio_stop"}, {"type":"capture_close"}]) + [{"type":"game_exit"},
                   {"type":"lighting_preset","preset":preset}]
        if returning and not preserve_output:
            effects.append(self._dialogue_prompt("DIALOGUE_RETURN", "Вернулись к разговору."))
        elif reason == "word_game_finished" and not preserve_output:
            effects.append(self._dialogue_prompt("DIALOGUE_FINISH", "Хорошо поиграли! Теперь можно выбрать другое занятие."))
        return self._decision(state, effects, reason, clear_audio=True)

    @staticmethod
    def _dialogue_prompt(content_id: str, text: str) -> dict[str, Any]:
        return {"type": "dialogue_prompt", "content_id": content_id, "text": text}

    def _activity_back(self, state, reason):
        returning = state.activity_return_mode == "bear_dialogue" and state.interaction_open
        state.mode = "bear_dialogue" if returning else "root_menu"
        state.activity_return_mode = None
        state.active_story_id = None
        state.story_checkpoint_confirmed = False
        state.dialogue_memory.story_checkpoint = None
        state.state_version += 1
        state.expected_inputs = (["speech", "button_7", "handset_down"] if returning else
                                 [f"button_{i}" for i in range(1, 8)] + ["handset_down"])
        effects = [{"type":"audio_stop"}, {"type":"capture_close"},
                   {"type":"lighting_preset", "preset":"BEAR_DIALOGUE" if returning else "ROOT_MENU"}]
        if returning:
            effects.append(self._dialogue_prompt("DIALOGUE_RETURN", "Вернулись к разговору."))
        return self._decision(state, effects, reason, clear_audio=True)

    def _information_effect(self, state, kind):
        if state.mode == 'bear_dialogue':
            state.dialogue_information_kind = kind
        return {'type':'typed_info_request', 'kind':kind}

    def _start_fixed_story(self, state, story_id):
        from .menu_content import catalogue_items
        available = dict(catalogue_items('fixed_story_menu', state.age_band, stories=self.stories))
        verified = self.stories.get_verified(story_id) if story_id in available else None
        if verified is None:
            return self._decision(state, [{"type":"content_blocked", "story_id":story_id},
                {"type":"speak", "phrase_id":"PHRASE_080"}], "fixed_story_blocked", clear_audio=True)
        state.active_story_id = story_id
        state.story_checkpoint_confirmed = False
        state.dialogue_memory.story_checkpoint = None
        state.state_version += 1
        if not state.interaction_open:
            state.pending_mode, state.mode = 'fixed_story_playing', 'awaiting_handset'
            return self._decision(state, [{"type":"visual", "view":"lift_handset"}], 'story_handset_required')
        state.mode = 'fixed_story_playing'
        state.expected_inputs = ['button_5','button_6','button_7','speech','handset_down']
        return self._decision(state, [{"type":"audio_stop"}, {"type":"capture_close"},
            {"type":"lighting_wave_intent", "pattern":"story_playback_wave", "groups":"story_playback", "source":"canonical", "loop":True, "stage_ms":620},
            {"type":"play_fixed_story", "story_id":story_id, "asset":verified['audio_file'], "duration_ms":verified['duration_ms']}],
            'fixed_story_started', clear_audio=True)

    def _start_game(self, state: CanonicalState, game_id: str, now: float, selection_button: int = 0,
                    *, from_dialogue: bool = False) -> dict[str, Any]:
        from_dialogue = from_dialogue or state.activity_return_mode == "bear_dialogue"
        selected = next((spec for spec in IMMERSIVE_GAME_SPECS if spec.game_id == game_id), None)
        if selected is None:
            return self._decision(state, [], "game_unavailable")
        policy = policy_for_band(state.age_band)
        engine = start_game(selected.game_id, sequence=0, now=now, selection_button=selection_button,
            held_buttons=tuple(state.accepted_buttons_down), run_id=uuid.uuid4().hex,
            response_window_s=policy.game_response_window_ms / 1000., first_hint_s=policy.first_hint_after_seconds,
            hint_limit=policy.repeat_count, age_band=state.age_band,
            level=state.game_progress.get(selected.game_id, 0), seed=state.game_entry_seed, immersive=True)
        if selected.mechanic == "sequence" and not state.interaction_open:
            engine = begin_demonstration(engine, now)
        state.game_engine_state = engine
        state.game_audio_stage = "starting" if state.interaction_open else "playing"
        if not state.interaction_open:
            state.game_engine_state = finish_prompt(engine, now).state
        state.game_return_mode = ("bear_dialogue" if from_dialogue
                                  else "game_menu" if state.mode == "game_menu"
                                  else None)
        state.activity_return_mode = "bear_dialogue" if from_dialogue else None
        state.pending_game_offer = state.game_offer_id = None
        state.game_offer_ready = False
        state.mode, state.active_game_id = "game_active", selected.game_id
        state.expected_inputs = ["button", "handset_down", "handset_up"]
        state.game_target = None
        state.game_level = engine.level
        state.game_parameters = {"game_id": selected.game_id, "engineering_parameter": True,
                                 "session_progress": True, "automatic_exit": False}
        state.state_version += 1
        effects = [{"type": "audio_stop"}, {"type": "capture_close"}, self._game_lighting(engine),
                   {"type": "game_started", "game_id": selected.game_id}]
        if state.interaction_open:
            effects.append({"type": "speak", "phrase_id": "PHRASE_020"})
        return self._decision(state, effects, "game_started", clear_audio=True)

    def _game_prompt(self, state: CanonicalState, text: str | None = None, feedback_id: str | None = None) -> dict[str, Any]:
        engine = state.game_engine_state
        challenge = current_challenge(engine)
        spec = next(item for item in IMMERSIVE_GAME_SPECS if item.game_id == engine.game_id)
        word_prompt = ("Сыграем небольшую партию. Назови животное или еду. Я назову другое."
            if state.age_band == "1-3" else
            "Сыграем небольшую партию. Называем слова на последнюю букву предыдущего. Мягкий и твёрдый знак, ы пропускаем. Назови первое слово."
            if state.age_band in {"5-7", "6-7"} else
            "Сыграем небольшую партию. Будем по очереди называть животных. Назови любое животное.")
        prompt = text or (challenge.prompt if challenge else {
            "free_lights":"Нажимай кнопки и смотри, как расходится свет.",
            "repeat_pattern":"Посмотри на огоньки. Потом повтори их порядок.",
            "word_game": word_prompt,
        }.get(engine.game_id, "Выбери ответ."))
        state.game_audio_stage = "prompt"
        return {"type":"game_prompt", "content_id":"GAME_PROMPT", "game_id":engine.game_id,
            "challenge_id":challenge.id if challenge else None, "prompt":prompt,
            "feedback_phrase_id":feedback_id, "feedback_text":"Получилось!" if feedback_id else None,
            "options":list(challenge.options) if challenge else [],
            "phrase_id":(challenge.phrase_id if challenge else spec.instruction_phrase_id
                         if engine.game_id in {'free_lights','repeat_pattern'} else None) if text is None else None,
            "answer_buttons":list(game_answer_buttons(engine)),
            "exit_hint": ("Чтобы выйти, держи семь. Когда скажу «Теперь отпускай», отпусти." if hold_exit_policy(engine)
                          else "Чтобы выйти, нажми семь.") if engine.round == 0 and text is None else None,
            "display":getattr(challenge, "display", "digits") if challenge else "colors"}

    def _game_input(self, state: CanonicalState, event: CanonicalEvent, now: float) -> dict[str, Any]:
        engine = state.game_engine_state
        if engine is None:
            return self._decision(state, [], "game_unavailable")
        immersive = getattr(engine, 'immersive', False)
        staged = immersive and engine.game_id != 'free_lights' and state.game_audio_stage in {'starting', 'prompt'}
        outcome = advance_game(engine, edge=event.event_type, button=int(event.payload["button_id"]),
                               sequence=engine.last_sequence + 1, now=now, paused=staged,
                               cancelled=event.payload.get('cancelled') is True)
        state.game_engine_state = outcome.state
        if outcome.status == "exit_requested":
            return self._game_exit(state, "game_exited_by_long_button")
        if outcome.status == 'exit_pending':
            state.game_audio_stage = 'idle'
            state.state_version += 1
            return self._decision(state, [{'type':'audio_stop'}, {'type':'capture_close'},
                {'type':'lighting_preset','preset':'GAME_EXIT_HOLD','step':0}], 'game_exit_pending', clear_audio=True)
        if outcome.status == 'exit_aborted':
            state.game_engine_state = replace(pause_game(outcome.state), phase='awaiting_prompt')
            state.state_version += 1
            effects = [{'type':'audio_stop'}, {'type':'capture_close'}, self._game_lighting(state.game_engine_state)]
            if state.interaction_open:
                prompt = self._game_prompt(state)
                prompt.update(prompt='Продолжаем игру. ' + prompt['prompt'], phrase_id=None, exit_hint=None)
                effects.append(prompt)
            else:
                state.game_engine_state = finish_prompt(state.game_engine_state, now).state
                state.game_audio_stage = 'playing'
            return self._decision(state, effects, 'game_exit_aborted', clear_audio=True)
        if outcome.state.seven_exit_phase != 'idle':
            return self._decision(state, [], 'game_exit_gesture_held')
        if (immersive and not state.interaction_open and outcome.speech_text
                and outcome.state.phase == "awaiting_prompt"):
            outcome = replace(outcome, state=finish_prompt(outcome.state, now).state)
            state.game_engine_state = outcome.state
        if immersive and engine.game_id == 'word_game':
            # Unrelated keys are not a new speech turn or scene owner. In
            # particular, they cannot revoke a running word response/history.
            return self._decision(state, [], 'game_' + outcome.status)
        state.game_target = None if immersive else outcome.state.target if outcome.state.game_id != "visual_free_play" else None
        if immersive:
            state.game_level = outcome.state.level
        # Key levels still update while narration owns this prompt, but they
        # must not invalidate that narration's semantic state/version lease.
        if staged:
            return self._decision(state, [], "game_prompt_waiting")
        state.state_version += 1
        if immersive:
            state.game_progress[engine.game_id] = max(state.game_progress.get(engine.game_id, 0),
                                                      getattr(outcome.state, 'level', 0))
            effects = []
            # A release must not replace an in-flight free-light ripple.
            if outcome.wave_origin is not None:
                effects.append({"type":"game_lighting", "animation":"ripple", "origin_button":outcome.wave_origin})
            elif engine.game_id != 'free_lights':
                effects.append(self._game_lighting(outcome.state))
            if outcome.speech_text and state.interaction_open:
                effects = [{"type":"audio_stop"}, {"type":"capture_close"},
                           self._game_prompt(state, outcome.speech_text, outcome.feedback_id)]
            elif outcome.feedback_id and state.interaction_open:
                effects.append({"type":"speak", "phrase_id":outcome.feedback_id})
            return self._decision(state, effects, "game_" + outcome.status)
        effects = [self._game_lighting(outcome.state)]
        if (engine.game_id == "repeat_pattern" and event.event_type == "ButtonPressed"
                and outcome.status in {"sequence_partial", "sequence_retry", "progress", "completed"}):
            effects.insert(0, {"type": "audio_stop"})
        if outcome.feedback_id and not outcome.completed and state.interaction_open:
            effects.append({"type": "speak", "phrase_id": outcome.feedback_id})
        if outcome.completed:
            state.mode = "root_menu"
            effects.append({"type": "lighting_preset", "preset": "ROOT_MENU"})
            if state.interaction_open:
                effects.append({"type": "speak", "phrase_id": "PHRASE_028"})
        return self._decision(state, effects, "game_" + outcome.status)

    def _public_story_after_expiry(self, state, effects, public):
        # Only the application supplies this verified, private-free receipt.
        state.active_story_id = public["story_id"]
        offset = public["offset_ms"]
        state.public_story_checkpoint_pending = offset is None
        state.story_checkpoint_confirmed = offset is not None
        state.mode = "story_paused"
        state.expected_inputs = ["button_7", "handset_down"]
        if offset is not None:
            state.dialogue_memory.story_checkpoint = {"story_id": public["story_id"], "offset_ms": offset}
            state.expected_inputs.insert(0, "button_6")
            if public["resume"]:
                state.mode = "fixed_story_playing"
                effects.append({"type": "story_audio_resume", "story_id": public["story_id"],
                                "checkpoint": state.dialogue_memory.story_checkpoint})
        effects.extend([{"type": "lighting_dark"}, {"type": "lighting_select_button", "button": 7}])
        if offset is not None:
            effects.append({"type": "lighting_select_button", "button": 6})
        return self._decision(state, effects, "public_story_identity_expired", clear_audio=True)

    @staticmethod
    def _session_action_noop(state, reason):
        return {"reason": reason, "state": state.snapshot(), "effects": [],
                "guards": {"expected_state_version": state.state_version}, "reset_session": False}

    def _decision(self, state: CanonicalState, effects: list[dict[str, Any]], reason: str, *,
                  reset_session: bool = False, clear_audio: bool = False) -> dict[str, Any]:
        if (state.mode not in {"game_menu", "game_active", "fixed_story_menu", "fixed_story_playing",
                "story_speech_analysis", "story_pause_announcement", "story_paused", "story_resume_announcement",
                "fixed_story_choice_wave"} or not state.interaction_open or state.safety_severity != "none"):
            state.activity_return_mode = None
        if state.mode != "bear_dialogue" or state.safety_severity != "none":
            state.dialogue_information_kind = None
        if state.mode not in {"personal_story_generating", "personal_story_validating", "personal_story_playing",
                              "story_pause_announcement", "story_paused", "story_resume_announcement"}:
            if state.active_story_id == "PERSONAL_STORY":
                state.active_story_id = None
            checkpoint = state.dialogue_memory.story_checkpoint
            if isinstance(checkpoint, dict) and checkpoint.get("story_id") == "PERSONAL_STORY":
                state.dialogue_memory.story_checkpoint = None
                state.story_checkpoint_confirmed = False
        if state.mode != "bear_dialogue" or not state.interaction_open or state.safety_severity != "none":
            state.pending_game_offer = state.game_offer_id = None
            state.game_offer_ready = False
        if state.mode != "game_active" or not state.interaction_open or state.safety_severity != "none":
            state.game_return_mode = None
            if (state.game_engine_state is not None and state.game_engine_state.seven_exit_phase != 'idle'
                    and (state.mode != 'game_active' or state.safety_severity != 'none')):
                state.game_engine_state = cancel_exit_gesture(state.game_engine_state)
        if state.mode != "story_paused":
            state.public_story_checkpoint_pending = False
        if state.mode != "game_active":
            state.game_engine_state = None
            state.active_game_id = None
            state.game_audio_stage = "idle"
            state.game_target = None
            state.game_parameters = {}
        effects = self._prepare_scene_menu(state, effects, reason)
        # Acknowledgements, audio completion and handset changes cannot answer
        # an exit question. Its explicit 6/7 choices change substate/mode first.
        if (not (state.mode == "game_active"
                 and state.substate == "game_exit_confirmation")
                and (state.mode != "safety_stop" or effects)):
            state.substate = reason
        state.timeout_at = state.dialogue_memory.expires_at
        audio_effects = [item for item in effects if item.get("type") in {
            "speak", "play_asset", "play_fixed_story", "resume_fixed_story", "play_placeholder",
            "story_audio_resume", "speak_dynamic", "support_question", "support_response", "play_private_welcome", "typed_info_request", "play_personal_story", "narrate_scene_menu", "game_prompt", "dialogue_prompt",
        }]
        if audio_effects:
            for audio_effect in audio_effects:
                if audio_effect.get("type") == "typed_info_request":
                    audio_effect["content_id"] = "TYPED_" + str(audio_effect["kind"]).upper()
                if audio_effect.get("type") == "support_question":
                    audio_effect["phrase_id"] = {
                        "pain_or_discomfort": "PHRASE_084", "fear": "PHRASE_085",
                    }.get(str(audio_effect.get("question")), "PHRASE_094")
                elif audio_effect.get("type") == "support_response":
                    audio_effect["phrase_id"] = (
                        "PHRASE_071" if str(audio_effect.get("kind")) == "concern"
                        else "PHRASE_094"
                    )
                if audio_effect.get("type") == "game_prompt":
                    audio_effect["phrase_asset_id"] = audio_effect.pop("phrase_id", None)
                content_id = str(audio_effect.get("phrase_id") or audio_effect.get("story_id")
                                 or audio_effect.get("content_id") or "dynamic")
                if not state.interaction_open:
                    audio_effect["deferred_until_handset"] = True
                    continue
                audio_effect["playback_lease"] = f"{state.session_id}:{uuid.uuid4().hex}"
                state.active_audio_id = content_id
                state.playback_lease = audio_effect["playback_lease"]
            state.audio_status = "queued" if state.interaction_open else "idle"
            if not state.interaction_open:
                state.active_audio_id = state.playback_lease = None
            state.audio_failure_code = None
        elif clear_audio or any(item.get("type") == "audio_stop" for item in effects):
            state.active_audio_id = None
            state.playback_lease = None
            state.audio_status = (
                "failed" if state.audio_status == "failed"
                else "cancelled" if any(item.get("type") == "audio_stop" for item in effects)
                else "idle"
            )
        return {"reason": reason, "state": state.snapshot(), "effects": effects,
                "guards": {"expected_state_version": state.state_version},
                "reset_session": bool(reset_session)}

    def _admit(self, event: CanonicalEvent, state: CanonicalState) -> str | None:
        if event.correlation_id and event.correlation_id in state.seen_correlations:
            return "idempotent_duplicate"
        if event.state_version != state.state_version:
            return "stale_state_version"
        if event.sequence <= state.last_sequence_by_source.get(event.source, -1):
            return "stale_sequence"
        state.last_sequence_by_source[event.source] = event.sequence
        if event.correlation_id:
            state.seen_correlations.add(event.correlation_id)
        return None

    def reserve_voice(self, event: CanonicalEvent) -> tuple[_VoiceAdmission | None, str | None]:
        """Admit once before async classification; no transition or effects yet."""
        event.validate()
        if event.event_type != "VoiceIntentReady":
            raise ValueError("only speech can be reserved")
        state = self.sessions.get(event.session_id)
        if state is None:
            state = self._new_state(event.session_id, None)
            self.sessions[event.session_id] = state
        reason = self._admit(event, state)
        if reason:
            return None, reason
        return _VoiceAdmission((event.session_id, event.source, event.sequence, event.correlation_id)), None

    def handle(self, event: CanonicalEvent, profile: Mapping[str, Any] | None = None, *,
               transient_text: str | None = None,
               _voice_admission: _VoiceAdmission | None = None,
               dialogue_fallback_authorized: bool = False,
               personal_story_authorized: bool = False,
               identity_event_authorized: bool = False,
               session_action_authorized: bool = False,
               identity_context_present: bool = False,
               public_story_expiry: dict | None = None,
               public_story_checkpoint_authorized: bool = False,
               game_timer_authorized: bool = False,
               interaction_idle_authorized: bool = False,
               private_welcome_ready: bool = False,
               personal_welcome_answer_verified: bool = False,
               game_confirmation_authorized: bool = False,
               catalogue_descriptor: dict | None = None) -> dict[str, Any]:
        event.validate()
        now = event.timestamp or self.now()
        monotonic_now = event.monotonic_timestamp if event.monotonic_timestamp is not None else self.monotonic_now()
        state = self.sessions.get(event.session_id)
        if state is None:
            # A ready card enters only through the private JSON boundary.
            initial_profile = None if event.event_type in {"ClientProfileReady", "ClientProfileCleared"} else profile
            state = self._new_state(event.session_id, initial_profile)
            self.sessions[event.session_id] = state
        # Authority is an out-of-band server argument, never event.source or
        # client-controlled payload. Untrusted resolve does not consume sequence.
        if event.event_type in {"SessionStarted", "InterviewSkipped"} and not session_action_authorized:
            return self._session_action_noop(state, "session_action_untrusted")
        if event.event_type in {"ClientProfileReady", "ClientProfileCleared", "IdentityReadyExpired", "IdentityAuthorizationExpired"} and not identity_event_authorized:
            return self._decision(state, [], "identity_outcome_untrusted")
        if event.event_type == "InteractionIdleExpired" and not interaction_idle_authorized:
            return self._decision(state, [], "interaction_idle_untrusted")
        if event.event_type == "GameTimer" and not game_timer_authorized:
            return self._decision(state, [], "game_timer_untrusted")
        if event.event_type == "PersonalStoryReady" and not personal_story_authorized:
            return self._decision(state, [], "personal_story_result_untrusted")
        if event.event_type == "DialogueFallback" and not dialogue_fallback_authorized:
            return self._decision(state, [], "dialogue_fallback_untrusted")
        if _voice_admission is None:
            reason = self._admit(event, state)
            if reason:
                return self._decision(state, [], reason)
        else:
            # This single-use receipt is private server state, never payload.
            if (event.event_type != "VoiceIntentReady" or _voice_admission.consumed
                    or _voice_admission.key != (event.session_id, event.source, event.sequence, event.correlation_id)):
                raise ValueError("invalid voice admission")
            _voice_admission.consumed = True
        if state.mode == "safety_stop":
            if event.event_type in {"HandsetPlaced", "SessionCancel", "SessionReset"}:
                return self._finish_local_safety(state, event,
                    panel_end=session_action_authorized and event.event_type == "SessionCancel")
            if event.event_type == "VoiceIntentReady":
                severity = str(event.payload.get("safety_severity", "none"))
                if severity == "urgent" and state.safety_severity != "urgent":
                    state.safety_severity = "urgent"
                    state.state_version += 1
                    return self._decision(state, [], "safety_severity_raised")
            elif event.event_type == "HandsetLifted" and state.handset != "up":
                state.handset = "up"
                state.state_version += 1
                return self._decision(state, [], "safety_stop_handset_lifted")
            elif event.event_type in {"AudioStarted", "AudioCompleted", "AudioFailed"}:
                # Only the current local guide owns audio state. Late playback
                # callbacks from the interrupted scene cannot alter this stop.
                if (state.active_audio_id == "PHRASE_091"
                        and event.payload.get("response_id") == "PHRASE_091"
                        and state.playback_lease
                        and event.payload.get("playback_lease") == state.playback_lease):
                    if event.event_type == "AudioStarted":
                        if state.audio_status != "playing":
                            state.audio_status = "playing"
                            state.state_version += 1
                        return self._decision(state, [], "safety_guidance_started")
                    failed = event.event_type == "AudioFailed"
                    state.audio_status = "failed" if failed else "finished"
                    state.audio_failure_code = str(event.payload.get("error_code") or "audio_failed")[:80] if failed else None
                    state.state_version += 1
                    return self._decision(state, [], "safety_guidance_failed" if failed else "safety_guidance_finished", clear_audio=True)
            if event.event_type not in {
                "HardwareDisconnected", "HardwareReconnected", "ClientProfileCleared",
                "IdentityReadyExpired", "IdentityAuthorizationExpired",
            }:
                # Sequence/correlation admission remains live; scenario state,
                # revision, LEDs and the one guidance recording stay unchanged.
                return {"reason": "safety_stop_active", "state": state.snapshot(), "effects": [],
                        "guards": {"expected_state_version": state.state_version}, "reset_session": False}
        if event.event_type == "HardwareDisconnected":
            return self._enter_local_safety(state, severity=state.safety_severity,
                                            reason="hardware_disconnected", guidance=False)
        fresh_button_press = False
        if event.event_type in {"ButtonPressed", "ButtonReleased"}:
            button = int(event.payload["button_id"])
            fresh_button_press = event.event_type == "ButtonPressed" and button not in state.accepted_buttons_down
            if event.event_type == "ButtonPressed" and button not in state.accepted_buttons_down:
                state.accepted_buttons_down.append(button)
            elif event.event_type == "ButtonReleased" and button in state.accepted_buttons_down:
                state.accepted_buttons_down.remove(button)
        if event.event_type == "HandsetPlaced" and state.handset == "down":
            return self._decision(state, [], "handset_already_placed")
        if event.event_type == "HandsetLifted" and state.handset == "up":
            return self._decision(state, [], "handset_already_lifted")
        if event.event_type != "TimeoutFired" and state.dialogue_memory.expires_at and now >= state.dialogue_memory.expires_at:
            state.dialogue_memory.clear()
            if state.mode in {"dialogue_suspended_ram", "story_suspended_ram"}:
                state.mode = "dialogue_memory_expired"
        effects: list[dict[str, Any]] = []
        if event.event_type == "InteractionIdleExpired":
            if state.safety_severity == "urgent" or state.mode == "safety_stop":
                return self._decision(state, [], "interaction_idle_safety_preserved")
            state.interaction_idle = True
            if self.exhibition_mode:
                state.dialogue_memory.clear()
                state.history_tail.clear()
            elif state.dialogue_memory.status != "suspended":
                state.dialogue_memory.suspend(now)
            state.mode = "root_menu"
            state.expected_inputs = ["button", "handset_up", "handset_down"]
            state.state_version += 1
            return self._decision(state, [{"type": "audio_stop"}, {"type": "capture_close"},
                                         {"type": "lighting_dark"}], "interaction_idle", clear_audio=True)
        if event.event_type == "ButtonReleased":
            button = int(event.payload["button_id"])
            if button in state.readiness_held_buttons:
                state.readiness_held_buttons.remove(button)
                return self._decision(state, [], "readiness_button_released")
        if (event.event_type in {"ButtonHeld", "ButtonLongHeld"}
                and int(event.payload['button_id']) in state.readiness_held_buttons):
            return self._decision(state, [], "readiness_button_held")
        if event.event_type == "ButtonPressed":
            button = int(event.payload["button_id"])
            if button in state.readiness_held_buttons:
                return self._decision(state, [], "readiness_button_held")
            ready = state.mode == "profile_ready" or (state.interaction_idle and state.handset == "down" and bool(state.identity_request_id))
            if ready and self.exhibition_mode:
                waking = state.interaction_idle
                state.readiness_held_buttons.append(button)
                state.identity_ready_deadline = None
                state.interaction_idle = False
                if self.exhibition_mode:
                    state.mode = "profile_ready"
                    state.state_version += 1
                    return self._decision(state, [{"type": "profile_ready_lighting"}] if waking else [], "profile_ready_engaged")
            state.interaction_idle = False
        if event.event_type == "HandsetLifted" and state.interaction_idle:
            state.interaction_idle = False
            state.mode = "idle"
        if self.optional_identity and event.event_type == "ClientProfileReady":
            if state.mode not in {"idle", "profile_ready"} and not (
                state.mode == "session_closed" and state.substate == "soft_end" and state.interaction_route is None
            ):
                return self._decision(state, [], "profile_active_session_ignored")
            if not profile or float(event.payload.get("expires_monotonic") or 0) <= monotonic_now:
                return self._decision(state, [], "profile_expired")
            fresh = self._new_state(state.session_id, profile)
            for name in ("profile_ref", "profile_status", "display_name", "grammatical_gender", "language", "timezone", "age_band", "age_band_source", "age_years", "age_policy", "profile_data_incomplete", "current_date", "accessibility_policy", "schedule_ref"):
                setattr(state, name, getattr(fresh, name))
            state.interaction_idle = False
            state.identity_request_id = event.payload["request_id"]
            state.identity_capabilities = ["personalize"]
            state.identity_expires_monotonic = float(event.payload["expires_monotonic"])
            state.identity_ready_deadline = min(float(event.payload["ready_deadline"]), state.identity_expires_monotonic)

            state.mode, state.expected_inputs = "profile_ready", ["handset_up"]
            state.state_version += 1
            if state.interaction_open:
                state.identity_ready_deadline = None
                return self._start_exhibition_welcome(state, monotonic_now, reason="profile_ready")
            return self._decision(state, [{"type": "profile_ready_lighting"}], "profile_ready")
        if self.optional_identity and event.event_type == "ClientProfileCleared":
            if not self.has_private_context(state) and not identity_context_present:
                return self._decision(state, [], "profile_already_clear")
            self.clear_identity(state)
            state.state_version += 1
            effects = [{"type": "audio_stop"}, {"type": "capture_close"}, {"type": "identity_cleared"}]
            if public_story_expiry:
                return self._public_story_after_expiry(state, effects, public_story_expiry)
            if state.mode == "safety_stop":
                return self._decision(state, effects + [{"type": "lighting_preset", "preset": "SAFE_STOP"}], "profile_cleared_safety_preserved")
            state.mode = "session_closed" if state.handset == "up" else "idle"
            state.expected_inputs = ["handset_down"] if state.handset == "up" else ["handset_up"]
            return self._decision(state, effects + [{"type": "lighting_dark"}], "profile_cleared")
        if self.optional_identity and event.event_type in {"IdentityReadyExpired", "IdentityAuthorizationExpired"}:
            if not state.identity_request_id or event.payload.get("request_id") != state.identity_request_id:
                return self._decision(state, [], "stale_identity_expiry")
            deadline = state.identity_ready_deadline if event.event_type == "IdentityReadyExpired" else state.identity_expires_monotonic
            if deadline is None or monotonic_now < deadline:
                return self._decision(state, [], "identity_expiry_not_due")
            self.clear_identity(state)
            state.state_version += 1
            effects = [{"type": "audio_stop"}, {"type": "capture_close"}, {"type": "identity_cleared"}]
            if public_story_expiry:
                return self._public_story_after_expiry(state, effects, public_story_expiry)
            if state.mode == "safety_stop":
                return self._decision(state, effects + [{"type": "lighting_preset", "preset": "SAFE_STOP"}], "identity_expired_safety_preserved")
            state.mode = "session_closed" if state.handset == "up" else "idle"
            state.expected_inputs = ["handset_down"] if state.handset == "up" else ["handset_up"]
            state.interaction_route = None
            return self._decision(state, effects + [{"type": "lighting_dark"}], "identity_expired")
        if (self.optional_identity or state.substate == "soft_end") and state.mode == "session_closed" and event.event_type == "HandsetPlaced":
            state.handset, state.mode, state.expected_inputs = "down", "idle", ["handset_up"]
            state.state_version += 1
            return self._decision(state, [{"type": "lighting_dark"}], "closed_handset_placed")
        if event.event_type in {"SessionCancel", "SessionReset"}:
            self.clear_identity(state)
            state.interaction_route = None
            state.mode = ("session_closed" if state.handset == "up" else "idle") if self.optional_identity else ("soft_end" if event.event_type == "SessionCancel" else "session_closed")
            if session_action_authorized and event.event_type == "SessionCancel":
                state.mode = "session_closed"
            state.expected_inputs = ["handset_down"] if state.handset == "up" else ["handset_up"]
            state.state_version += 1
            lighting = {"type": "lighting_dark"} if self.optional_identity else {"type": "lighting_preset", "preset": "READY"}
            return self._decision(state, [{"type": "audio_stop"}, {"type": "capture_close"}, {"type": "identity_cleared"}, lighting], "soft_end" if event.event_type == "SessionCancel" else "session_closed")
        if event.event_type == "SessionStarted":
            if state.substate == "profile_cleanup_failed":
                return self._session_action_noop(state, "session_start_cleanup_failed")
            if state.interaction_open and state.mode not in {"idle", "profile_ready", "soft_end", "session_closed"}:
                return self._session_action_noop(state, "interaction_already_started")
            state.interaction_route = "panel"
            state.interaction_idle = False
            state.identity_ready_deadline = None
            state.state_version += 1
            return self._start_exhibition_welcome(state, monotonic_now, reason="panel_session_started")
        if event.event_type == "InterviewSkipped":
            if (not state.interaction_open or state.profile_status == "identified"
                    or state.mode not in {"exhibition_introduction", "exhibition_name_question",
                        "exhibition_name_capture", "exhibition_age_prompt", "exhibition_age_hint", "exhibition_age_selection"}):
                return self._session_action_noop(state, "interview_skip_unavailable")
            return self._personal_welcome_menu(state, "exhibition_interview_skipped")
        # A closed/soft-ended session is inert until an authorized profile or explicit
        # anonymous start creates a fresh scenario context. Stray handset,
        # audio and button events must not resurrect it.
        if state.mode in {"soft_end", "session_closed"} and event.event_type not in {"AnonymousStart", "SessionReset"}:
            if not (state.mode == "session_closed" and state.handset == "down"
                    and state.substate == "soft_end" and event.event_type == "HandsetLifted"):
                return self._decision(state, [], "session_closed")
        if event.event_type == "ExhibitionOnboardingComplete":
            if not self.exhibition_mode or state.mode != "exhibition_onboarding_complete":
                return self._decision(state, [], "exhibition_menu_handoff_ignored")
            # The onboarding closing wave is already complete and dark here.
            state.mode, state.substate = "exhibition_menu_tutorial_buttons", "awaiting_menu_audio"
            state.expected_inputs = ["audio_completed", "handset_down"]
            state.menu_tutorial_step = 0
            state.menu_tutorial_phase = "idle"
            state.menu_tutorial_audio_complete = False
            state.menu_prompt_after_wave = True
            state.menu_lit_buttons = []
            state.menu_pressed_buttons = []
            state.menu_repeat_attempts = 0
            state.selected_menu_button = None
            state.selected_menu_action = None
            state.state_version += 1
            return self._decision(state, [
                {"type": "speak", "phrase_id": "PHRASE_008"},
                {"type": "exhibition_menu_tutorial_step", "step": "menu_audio"},
            ], "exhibition_menu_tutorial_started")
        if event.event_type == "MenuTutorialStep":
            if not self.exhibition_mode or state.mode not in {
                "exhibition_menu_tutorial_buttons", "exhibition_menu_replay_buttons",
            }:
                return self._decision(state, [], "exhibition_menu_tutorial_step_ignored")
            phase = str(event.payload.get("phase") or "")
            # Legacy operator/test events remain accepted; production sends
            # explicit digit/full phases tied to the recorded WAV.
            if not phase:
                try:
                    step = int(event.payload.get("step"))
                except (TypeError, ValueError):
                    return self._decision(state, [], "exhibition_menu_tutorial_step_invalid")
                if step != state.menu_tutorial_step + 1 or step not in range(1, 8):
                    return self._decision(state, [], "exhibition_menu_tutorial_step_duplicate")
                state.menu_tutorial_step = step
                state.menu_tutorial_phase = "full"
                state.menu_lit_buttons = list(range(1, step + 1))
                state.state_version += 1
                lighting = {"type": "lighting_preset", "preset": "EXHIBITION_MENU_TUTORIAL",
                            "buttons": list(state.menu_lit_buttons), "tutorial_step": step}
                if step == 7:
                    state.mode, state.substate = "menu_ready", "menu_tutorial_complete"
                    state.expected_inputs = [f"button_{item}" for item in range(1, 8)] + ["handset_down"]
                    return self._decision(state, [
                        lighting,
                        {"type": "menu_tutorial_complete", "buttons": list(state.menu_lit_buttons)},
                    ], "menu_tutorial_complete")
                state.substate = f"explaining_button_{step}"
                state.expected_inputs = ["menu_tutorial_step", "handset_down"]
                return self._decision(state, [
                    lighting,
                    {"type": "exhibition_menu_tutorial_step", "step": step,
                     "button": step, "buttons": list(state.menu_lit_buttons)},
                ], "menu_tutorial_button_lit")

            try:
                button = int(event.payload.get("button"))
            except (TypeError, ValueError):
                return self._decision(state, [], "exhibition_menu_tutorial_step_invalid")
            expected = state.menu_tutorial_step + 1
            if button != expected or button not in range(1, 8):
                return self._decision(state, [], "exhibition_menu_tutorial_step_duplicate")
            if phase == "digit":
                if state.menu_tutorial_phase not in {"idle", "full"}:
                    return self._decision(state, [], "exhibition_menu_tutorial_phase_duplicate")
                state.menu_tutorial_phase = "digit"
                state.substate = f"explaining_button_{button}_digit"
                state.expected_inputs = ["menu_tutorial_step", "handset_down"]
                state.state_version += 1
                return self._decision(state, [
                    {"type": "lighting_preset", "preset": "EXHIBITION_MENU_TUTORIAL_DIGIT",
                     "buttons": list(state.menu_lit_buttons), "digit_button": button,
                     "tutorial_phase": "digit"},
                    {"type": "exhibition_menu_tutorial_step", "button": button,
                     "phase": "digit", "buttons": list(state.menu_lit_buttons)},
                ], "menu_tutorial_digit_lit")
            if phase == "full":
                if state.menu_tutorial_phase != "digit":
                    return self._decision(state, [], "exhibition_menu_tutorial_phase_invalid")
                state.menu_tutorial_step = button
                state.menu_tutorial_phase = "full"
                state.menu_lit_buttons = list(range(1, button + 1))
                state.substate = f"explaining_button_{button}"
                state.expected_inputs = ["menu_tutorial_step", "audio_completed", "handset_down"]
                state.state_version += 1
                return self._decision(state, [
                    {"type": "lighting_preset", "preset": "EXHIBITION_MENU_TUTORIAL",
                     "buttons": list(state.menu_lit_buttons), "tutorial_step": button,
                     "tutorial_phase": "full"},
                    {"type": "exhibition_menu_tutorial_step", "button": button,
                     "phase": "full", "buttons": list(state.menu_lit_buttons)},
                ], "menu_tutorial_button_lit")
            return self._decision(state, [], "exhibition_menu_tutorial_phase_invalid")
        if event.event_type == "WelcomeTimerFired":
            deadline = state.welcome_deadline_monotonic
            if state.mode != "voice_welcome" or state.welcome_status != "pending" or deadline is None:
                return self._decision(state, [], "welcome_timer_not_pending")
            if monotonic_now < deadline:
                return self._decision(state, [], "welcome_not_due")
            state.welcome_status = "started"
            state.state_version += 1
            if private_welcome_ready:
                return self._decision(state, [{"type": "welcome_ring_stop"}, {
                    "type": "play_private_welcome", "content_id": "PERSONAL_WELCOME",
                }], "personal_welcome_started")
            return self._decision(state, [{"type": "welcome_ring_stop"}, {
                "type": "speak", "phrase_id": "PHRASE_001",
                "scheduled_deadline_monotonic": deadline,
                "started_at_monotonic": monotonic_now,
            }], "welcome_started")
        if event.event_type == "GameTimer":
            engine = state.game_engine_state
            if (state.mode != "game_active" or state.substate == "game_exit_confirmation" or engine is None
                    or event.payload.get("run_id") != engine.run_id
                    or event.payload.get("round_generation") != engine.round_generation
                    or event.payload.get("timer_generation") != engine.timer_generation
                    or event.payload.get('gesture_generation', 0) != engine.seven_gesture_generation
                    or event.payload.get("deadline") != next_deadline(engine)):
                return self._decision(state, [], "game_timer_stale")
            outcome = timer_step(engine, monotonic_now)
            if (not state.interaction_open and outcome.speech_text and outcome.state.phase == "awaiting_prompt"):
                outcome = replace(outcome, state=finish_prompt(outcome.state, monotonic_now).state)
            if (outcome.feedback_id == "PHRASE_041" and state.interaction_open and not state.active_audio_id):
                outcome = replace(outcome, state=replace(outcome.state, phase="instruction", demonstration_deadline=None))
            state.game_engine_state = outcome.state
            state.state_version += 1
            if outcome.status in {'exit_progress','exit_ready'}:
                effects = [{'type':'lighting_preset','preset':'GAME_EXIT_HOLD','step':outcome.state.seven_exit_step}]
                if outcome.status == 'exit_ready' and state.interaction_open:
                    effects.append({'type':'speak','phrase_id':'PHRASE_036'})
                return self._decision(state, effects, 'game_' + outcome.status)
            effects = [{"type": "game_wait", "reason": outcome.status,
                        "hints_emitted": outcome.state.hints_emitted}]
            if getattr(engine, 'immersive', False) and outcome.speech_text and state.interaction_open:
                return self._decision(state, [{'type':'audio_stop'}, {'type':'capture_close'},
                    self._game_prompt(state, outcome.speech_text, outcome.feedback_id)], 'game_' + outcome.status)
            if outcome.status in {"sequence_demonstration", "sequence_input"}:
                effects.append(self._game_lighting(outcome.state))
            # Do not replace an instruction still playing or fabricate audio
            # on the silent clinical on-hook visual path.
            if outcome.feedback_id and state.interaction_open and not state.active_audio_id:
                effects.append({"type": "speak", "phrase_id": outcome.feedback_id})
            return self._decision(state, effects, "game_" + outcome.status)
        if event.event_type == "TimeoutFired":
            if state.mode == "game_active" and state.substate == "game_exit_confirmation":
                return self._decision(state, [], "game_exit_confirmation_pending")
            if state.mode == "game_active":
                return self._decision(state, [], "game_waiting")
            if state.dialogue_memory.expires_at and now >= state.dialogue_memory.expires_at:
                state.dialogue_memory.clear()
                if state.mode in {"dialogue_suspended_ram", "story_suspended_ram"}:
                    state.mode = "dialogue_memory_expired"
                state.state_version += 1
                return self._decision(state, [{"type": "audio_stop"}, {"type": "clear_private_display"}], "ram_ttl_expired")
            return self._decision(state, [], "timeout_not_due")
        if event.event_type == "PersonalStoryReady":
            if state.mode != "personal_story_generating" or not state.interaction_open:
                return self._decision(state, [], "personal_story_result_stale")
            state.mode = "personal_story_playing"
            state.active_story_id = "PERSONAL_STORY"
            state.state_version += 1
            state.expected_inputs = ["button_5", "button_6", "button_7", "handset_down"]
            story_wave = {"type": "lighting_wave_intent", "pattern": "story_playback_wave", "groups": "story_playback", "source": "canonical", "loop": True, "stage_ms": 620}
            return self._decision(state, [story_wave, {"type":"play_personal_story", "content_id":"PERSONAL_STORY"}], "personal_story_ready")
        if event.event_type == "DialogueFallback":
            if event.payload.get('reason') == 'catalogue_request':
                if state.mode != 'bear_dialogue' or not state.interaction_open or state.safety_severity != 'none':
                    return self._decision(state, [], 'catalogue_request_stale')
                action, target = event.payload.get('action'), event.payload.get('target')
                if action == 'open_catalog' and target in {'games','stories'}:
                    state.activity_return_mode = 'bear_dialogue'
                    state.mode = 'game_menu' if target == 'games' else 'fixed_story_menu'
                    state.state_version += 1
                    return self._decision(state, [{'type':'audio_stop'}, {'type':'capture_close'}], 'voice_open_activity', clear_audio=True)
                if action == 'play_story':
                    state.activity_return_mode = 'bear_dialogue'
                    return self._start_fixed_story(state, target)
                if action == 'request_information' and target in {'date','weather','what_next','where_now','card_doctors'}:
                    state.state_version += 1
                    return self._decision(state, [{'type':'audio_stop'}, {'type':'capture_close'},
                        self._information_effect(state, target)], 'typed_info_requested', clear_audio=True)
                return self._decision(state, [], 'catalogue_request_invalid')
            if event.payload.get("reason") == "game_offer":
                game_id = event.payload.get("game_id")
                selected = next((spec for spec in IMMERSIVE_GAME_SPECS if spec.game_id == game_id), None)
                if state.mode != "bear_dialogue" or not state.interaction_open or selected is None or state.safety_severity != "none":
                    return self._decision(state, [], "game_offer_stale")
                state.pending_game_offer = selected.game_id
                state.game_offer_id = uuid.uuid4().hex
                state.game_offer_ready = False
                state.expected_inputs = ["audio_completed", "button_7", "handset_down"]
                state.state_version += 1
                prompt = f"Поиграем в «{selected.display_title}»? Да — один, отмена — семь. Можно ответить голосом."
                return self._decision(state, [{"type": "audio_stop"}, {"type": "capture_close"},
                    {"type": "lighting_preset", "preset": "GAME_OFFER"},
                    self._dialogue_prompt("DIALOGUE_OFFER", prompt)], "game_offered", clear_audio=True)
            if (event.payload.get("reason") == "word_turn_complete" and state.mode == "game_active"
                    and state.active_game_id == "word_game" and state.game_engine_state is not None):
                engine = state.game_engine_state
                from .age_policy import POLICIES
                limit = POLICIES.get(state.age_band, POLICIES["3-5"]).dialogue_turn_limit
                if engine.round + 1 >= limit:
                    return self._game_exit(state, "word_game_finished", preserve_output=True)
                state.game_engine_state = arm_wait(replace(engine, round=engine.round + 1,
                    round_generation=engine.round_generation + 1), monotonic_now)
                state.state_version += 1
                return self._decision(state, [], "word_game_turn_finished")
            if (state.mode == "game_active" and state.active_game_id == "word_game"
                    and state.interaction_open):
                reason = str(event.payload.get('reason') or 'provider_or_validation_failure')
                if reason in {'bounded_limit', 'dialogue_exit'}:
                    return self._game_exit(state, 'word_game_finished')
                state.state_version += 1
                return self._decision(state, [{'type':'speak','phrase_id':'PHRASE_104'}],
                                      'word_game_retry', clear_audio=True)
            if (event.payload.get("reason") == "typed_information_unavailable"
                    and state.mode in {"schedule_weather", "bear_dialogue"} and state.interaction_open):
                state.state_version += 1
                phrase = "PHRASE_101" if event.payload.get("kind") in {"what_next", "where_now", 'card_doctors'} else "PHRASE_100"
                return self._decision(state, [{"type": "audio_stop"}, {"type": "speak", "phrase_id": phrase}],
                                      "typed_information_unavailable", clear_audio=True)
            if (event.payload.get("reason") == "personal_story_unavailable"
                    and state.mode in {"personal_story_generating", "personal_story_validating", "personal_story_playing"}
                    and state.interaction_open):
                # One owned failure recording, then a functioning fixed-story
                # chooser. Do not claim a generated story exists or queue two
                # prompts on the same playback lease.
                state.mode = "fixed_story_menu"
                state.expected_inputs = [f"button_{item}" for item in range(1, 6)] + ["button_7", "handset_down"]
                state.state_version += 1
                return self._decision(state, [
                    {"type": "audio_stop"},
                    {"type": "lighting_preset", "preset": "STORY_MENU"},
                    {"type": "speak", "phrase_id": "PHRASE_080"},
                ], "personal_story_unavailable", clear_audio=True)
            if state.mode not in {"bear_dialogue", "support_intake"} or not state.interaction_open:
                return self._decision(state, [], "dialogue_fallback_ignored")
            reason = str(event.payload.get("reason") or "provider_or_validation_failure")
            leaving = reason in {"bounded_limit", "dialogue_exit"}
            phrase_id = "PHRASE_095" if leaving else "PHRASE_104"
            if leaving:
                state.mode = "root_menu"
                state.expected_inputs = [f"button_{item}" for item in range(1, 8)] + ["handset_down"]
            state.state_version += 1
            return self._decision(state, [{"type": "speak", "phrase_id": phrase_id}],
                                  "dialogue_fallback", clear_audio=True)
        if event.event_type == "StoryPositionUpdated":
            if state.public_story_checkpoint_pending and not public_story_checkpoint_authorized:
                return self._decision(state, [], "story_checkpoint_ignored")
            if (state.mode not in {"story_pause_announcement", "story_paused"}
                    or str(event.payload.get("story_id") or "") != str(state.active_story_id or "")):
                return self._decision(state, [], "story_checkpoint_ignored")
            state.dialogue_memory.story_checkpoint = {"story_id": state.active_story_id, "offset_ms": max(0, int(event.payload.get("offset_ms", 0)))}
            state.story_checkpoint_confirmed = True
            state.state_version += 1
            if state.public_story_checkpoint_pending:
                state.public_story_checkpoint_pending = False
                state.expected_inputs = ["button_6", "button_7", "handset_down"]
                if event.payload.get("resume"):
                    state.mode = "fixed_story_playing"
                    return self._decision(state, [{"type": "story_audio_resume", "story_id": state.active_story_id,
                        "checkpoint": state.dialogue_memory.story_checkpoint}], "public_story_checkpoint_resumed")
            return self._decision(state, [], "story_checkpoint_updated")
        if event.event_type in {"AudioCompleted", "AudioFailed"}:
            response_id = str(event.payload.get("response_id") or event.payload.get("content_id") or "")
            if state.active_audio_id and response_id != state.active_audio_id:
                return self._decision(state, [], "event_ignored")
            if event.payload.get("playback_lease") and event.payload.get("playback_lease") != state.playback_lease:
                return self._decision(state, [], "event_ignored")
            if response_id in {"DIALOGUE_OFFER", "DIALOGUE_RETURN", "DIALOGUE_FINISH"}:
                if (not state.active_audio_id or not event.payload.get("playback_lease")
                        or event.payload["playback_lease"] != state.playback_lease):
                    return self._decision(state, [], "dialogue_prompt_stale_audio")
                state.state_version += 1
                if response_id == "DIALOGUE_OFFER" and state.pending_game_offer:
                    # The same physical controls remain available if speech fails.
                    state.game_offer_ready = True
                    state.expected_inputs = ["speech", "button_1", "button_7", "handset_down"]
                    return self._decision(state, [{"type": "capture_open"}], "game_offer_ready", clear_audio=True)
                return self._decision(state, [{"type": "capture_open"}] if state.mode == "bear_dialogue" else [],
                                      "dialogue_prompt_finished", clear_audio=True)
            menu_result = self._scene_menu_audio(state, event, response_id)
            if menu_result is not None: return menu_result
            if response_id == "RINGTONE" and state.mode == "voice_welcome" and state.welcome_status == "pending":
                # The optional ring must never complete the welcome or cancel
                # its timer. It still has an owned terminal playback state.
                state.audio_status = "finished" if event.event_type == "AudioCompleted" else "failed"
                state.state_version += 1
                return self._decision(state, [], "ringtone_finished" if event.event_type == "AudioCompleted"
                                      else "ringtone_failed", clear_audio=True)
            if (state.mode == "game_active" and state.game_engine_state is not None
                    and getattr(state.game_engine_state, 'immersive', False)
                    and response_id in {"PHRASE_020", "GAME_PROMPT"}):
                if (not state.active_audio_id or not event.payload.get('playback_lease')
                        or event.payload['playback_lease'] != state.playback_lease):
                    return self._decision(state, [], 'game_prompt_stale_audio')
                if response_id == 'PHRASE_020' and state.game_audio_stage == 'starting':
                    state.state_version += 1
                    return self._decision(state, [self._game_prompt(state)], 'game_prompt_started', clear_audio=True)
                if response_id == 'GAME_PROMPT' and state.game_audio_stage == 'prompt':
                    if event.event_type == 'AudioFailed':
                        if state.active_game_id == "word_game":
                            state.game_engine_state = arm_wait(replace(state.game_engine_state, phase="playing"),
                                                               monotonic_now, reset_hints=False)
                            state.game_audio_stage = "playing"
                            state.state_version += 1
                            return self._decision(state, [{"type": "capture_open"}],
                                                  "word_game_prompt_recovered", clear_audio=True)
                        return self._game_exit(state, 'game_prompt_audio_failed')
                    outcome = finish_prompt(state.game_engine_state, monotonic_now)
                    state.game_engine_state = outcome.state
                    state.game_audio_stage = 'playing'
                    state.expected_inputs = ['button','speech','handset_down']
                    state.state_version += 1
                    return self._decision(state, [self._game_lighting(outcome.state),
                        {'type':'capture_open'}], 'game_prompt_finished', clear_audio=True)
                return self._decision(state, [], 'game_prompt_terminal_ignored')
            if (state.mode == 'game_active' and state.active_game_id == 'word_game'
                    and response_id == 'PHRASE_104' and state.active_audio_id == 'PHRASE_104'):
                if (not event.payload.get('playback_lease')
                        or event.payload['playback_lease'] != state.playback_lease):
                    return self._decision(state, [], 'word_game_retry_stale_audio')
                state.audio_status = 'failed' if event.event_type == 'AudioFailed' else 'finished'
                if state.game_engine_state is not None:
                    state.game_engine_state = arm_wait(state.game_engine_state, monotonic_now, reset_hints=False)
                state.state_version += 1
                return self._decision(state, [{'type':'capture_open'}], 'word_game_retry_finished', clear_audio=True)
            game_instruction_ids = {spec.instruction_phrase_id for spec in AVAILABLE_GAME_SPECS} | {"PHRASE_036", "PHRASE_041", "PHRASE_042"}
            if ((state.mode == "game_active" and state.substate != "game_exit_confirmation"
                    and response_id in game_instruction_ids)
                    or (state.mode == "root_menu"
                        and response_id == "PHRASE_028")) and state.active_audio_id == response_id:
                # Optional instructions never own playable visual state. Only
                # this exact active lease may finish; no scoring/navigation.
                if not event.payload.get("playback_lease") or event.payload["playback_lease"] != state.playback_lease:
                    return self._decision(state, [], "game_instruction_stale_audio")
                failed = event.event_type == "AudioFailed"
                state.audio_status = "failed" if failed else "finished"
                state.audio_failure_code = "game_instruction_audio_unavailable" if failed else None
                state.state_version += 1
                effects = ([{"type": "content_blocked", "error_code": "game_instruction_audio_unavailable"}]
                           if failed else [])
                if (state.mode == "game_active" and state.interaction_open and response_id == "PHRASE_035"
                        and state.game_engine_state is not None and state.game_engine_state.hold_ready
                        and state.game_engine_state.target in state.game_engine_state.held):
                    effects.append({"type": "speak", "phrase_id": "PHRASE_036"})
                if (state.game_engine_state is not None and state.game_engine_state.phase == "instruction"
                        and state.game_engine_state.game_id == "repeat_pattern" and response_id in {"PHRASE_040", "PHRASE_041"}):
                    state.game_engine_state = begin_demonstration(state.game_engine_state, monotonic_now)
                    effects.append(self._game_lighting(state.game_engine_state))
                return self._decision(state, effects,
                    "game_instruction_audio_failed" if failed else "game_instruction_audio_finished", clear_audio=True)
            if (state.mode == "game_active" and state.substate == "game_exit_confirmation"
                    and response_id == "PHRASE_062" and state.active_audio_id == "PHRASE_062"):
                # This optional voice hint does not own the visual choice. A
                # failed speaker must not turn working buttons into a dead end.
                failed = event.event_type == "AudioFailed"
                state.audio_status = "failed" if failed else "finished"
                state.audio_failure_code = "game_exit_audio_unavailable" if failed else None
                state.state_version += 1
                effects = ([{"type": "content_blocked", "error_code": "game_exit_audio_unavailable"}]
                           if failed else [])
                return self._decision(state, effects,
                    "game_exit_audio_failed" if failed else "game_exit_audio_finished", clear_audio=True)
        if (event.event_type in {"AudioCompleted", "AudioFailed"} and state.mode == "personal_story_playing"
                and event.payload.get("response_id") == "PERSONAL_STORY"):
            if (state.active_audio_id != "PERSONAL_STORY" or not event.payload.get("playback_lease")
                    or event.payload["playback_lease"] != state.playback_lease):
                return self._decision(state, [], "personal_story_completion_stale")
            state.mode = "root_menu"
            state.active_story_id = None
            state.state_version += 1
            return self._decision(state, [{"type":"audio_stop"}, {"type":"lighting_preset","preset":"ROOT_MENU"}],
                                  "personal_story_finished" if event.event_type == "AudioCompleted" else "personal_story_playback_failed", clear_audio=True)
        if (event.event_type == "AudioCompleted" and state.mode == "schedule_weather"
                and (response_id.startswith("TYPED_") or response_id in {"PHRASE_100", "PHRASE_101"})):
            state.state_version += 1
            return self._decision(state, [], "typed_information_finished", clear_audio=True)
        if (event.event_type == 'AudioCompleted' and state.mode == 'bear_dialogue'
                and state.dialogue_information_kind and response_id in {
                    'TYPED_'+state.dialogue_information_kind.upper(), 'PHRASE_100', 'PHRASE_101'}):
            state.dialogue_information_kind = None
            state.state_version += 1
            return self._decision(state, [{'type':'capture_open'}], 'typed_information_finished', clear_audio=True)
        if (event.event_type == "AudioCompleted" and state.mode == "fixed_story_menu"
                and response_id == "PHRASE_080" and state.active_audio_id == "PHRASE_080"):
            if not event.payload.get("playback_lease") or event.payload["playback_lease"] != state.playback_lease:
                return self._decision(state, [], "personal_story_failure_stale_audio")
            state.state_version += 1
            return self._decision(state, [], "personal_story_failure_finished", clear_audio=True)
        if event.event_type == "AudioCompleted" and state.mode == "fixed_story_menu" and state.story_menu_hint_active:
            if (response_id != "PHRASE_075" or not event.payload.get("playback_lease")
                    or event.payload["playback_lease"] != state.playback_lease):
                return self._decision(state, [], "story_menu_hint_stale_audio")
            state.story_menu_hint_active = False
            return self._decision(state, [], "story_menu_hint_finished", clear_audio=True)
        if (event.event_type == "AudioCompleted" and state.mode == "personal_welcome_listen"
                and response_id == "PHRASE_074"):
            state.state_version += 1
            return self._decision(state, [{"type": "capture_open"}], "personal_welcome_retry_ready", clear_audio=True)
        if (event.event_type == "AudioCompleted" and state.mode == "voice_welcome"
                and response_id == "PERSONAL_WELCOME" and state.welcome_status == "started"):
            state.welcome_status = "completed"
            return self._personal_welcome_menu(state, "personal_welcome_menu_started")
        if event.event_type == "AudioCompleted" and self.exhibition_mode and state.mode == "voice_welcome":
            if str(event.payload.get("response_id") or "") != "PHRASE_001" or state.welcome_status != "started":
                return self._decision(state, [], "exhibition_welcome_completion_ignored")
            if (self.optional_identity and state.profile_status == "identified"
                    and state.profile_ref and state.identity_request_id
                    and "personalize" in state.identity_capabilities
                    and state.identity_expires_monotonic is not None
                    and state.identity_expires_monotonic > monotonic_now):
                # The validated JSON card owns personalization. Do not replace
                # its name/age with the exhibition interview, including when an
                # optional field is missing: the existing unknown-age policy is
                # a difficulty default, not a claim about this child's age.
                state.mode, state.substate = "exhibition_menu_tutorial_prompt", "awaiting_menu_prompt_audio"
                state.expected_inputs = ["audio_completed", "handset_down"]
                state.welcome_status = "completed"
                state.menu_prompt_after_wave = True
                state.state_version += 1
                return self._decision(state, [
                    {"type": "speak", "phrase_id": "PHRASE_007"},
                    {"type": "exhibition_onboarding_step", "step": "authorized_profile_ready"},
                ], "authorized_profile_menu_started", clear_audio=True)
            state.mode, state.substate = "exhibition_introduction", "awaiting_introduction_audio"
            state.expected_inputs = ["audio_completed", "handset_down"]
            state.welcome_status = "completed"
            state.state_version += 1
            return self._decision(state, [
                {"type": "speak", "phrase_id": "PHRASE_078"},
                {"type": "exhibition_onboarding_step", "step": "introduction"},
            ], "exhibition_introduction_started", clear_audio=True)
        if event.event_type == "AudioCompleted" and self.exhibition_mode and state.mode == "exhibition_introduction":
            if str(event.payload.get("response_id") or "") != "PHRASE_078":
                return self._decision(state, [], "exhibition_introduction_completion_ignored")
            state.mode, state.substate = "exhibition_name_question", "awaiting_name_question_audio"
            state.expected_inputs = ["audio_completed", "handset_down"]
            state.state_version += 1
            return self._decision(state, [
                {"type": "speak", "phrase_id": "PHRASE_099"},
                {"type": "exhibition_onboarding_step", "step": "name_question"},
            ], "exhibition_name_question_started", clear_audio=True)
        if event.event_type == "AudioCompleted" and self.exhibition_mode and state.mode == "exhibition_name_question":
            if str(event.payload.get("response_id") or "") != "PHRASE_099":
                return self._decision(state, [], "exhibition_name_question_completion_ignored")
            state.mode, state.substate = "exhibition_name_capture", "awaiting_name"
            state.expected_inputs = ["speech", "handset_down"]
            state.state_version += 1
            return self._decision(state, [
                {"type": "capture_open"},
                {"type": "exhibition_onboarding_step", "step": "name_capture"},
            ], "exhibition_name_capture_started", clear_audio=True)
        if event.event_type == "AudioCompleted" and self.exhibition_mode and state.mode == "exhibition_name_capture":
            if str(event.payload.get("response_id") or "") != "PHRASE_074":
                return self._decision(state, [], "exhibition_name_audio_completion_ignored")
            state.state_version += 1
            return self._decision(state, [{"type": "capture_open"}], "exhibition_name_retry_ready", clear_audio=True)
        if event.event_type == "AudioCompleted" and self.exhibition_mode and state.mode == "exhibition_age_prompt":
            if str(event.payload.get("response_id") or "") != "PHRASE_108":
                return self._decision(state, [], "exhibition_age_question_completion_ignored")
            state.mode, state.substate = "exhibition_age_hint", "awaiting_age_hint_audio"
            state.expected_inputs = ["audio_completed", "handset_down"]
            state.state_version += 1
            return self._decision(state, [
                {"type": "speak", "phrase_id": "PHRASE_075"},
                {"type": "exhibition_onboarding_step", "step": "age_button_hint"},
            ], "exhibition_age_hint_started", clear_audio=True)
        if event.event_type == "AudioCompleted" and self.exhibition_mode and state.mode == "exhibition_age_hint":
            if str(event.payload.get("response_id") or "") != "PHRASE_075":
                return self._decision(state, [], "exhibition_age_hint_completion_ignored")
            state.mode, state.substate = "exhibition_age_selection", "awaiting_age_button"
            state.expected_inputs = [*(f"button_{button}" for button in range(1, 8)), "speech", "handset_down"]
            state.state_version += 1
            return self._decision(state, [
                {"type": "lighting_wave_intent", "pattern": "age_digits_wave", "groups": "digits", "source": "canonical"},
                {"type": "capture_open"},
                {"type": "exhibition_onboarding_step", "step": "age_selection"},
            ], "exhibition_age_selection_started", clear_audio=True)
        if event.event_type == "AudioCompleted" and self.exhibition_mode and state.mode == "exhibition_age_complete":
            if str(event.payload.get("response_id") or "") != "PHRASE_107":
                return self._decision(state, [], "exhibition_closing_audio_completion_ignored")
            state.mode, state.substate = "exhibition_onboarding_complete", "awaiting_onboarding_wave"
            state.expected_inputs = ["onboarding_wave_complete", "handset_down"]
            state.state_version += 1
            return self._decision(state, [
                {"type": "lighting_wave_intent", "pattern": "onboarding_closing_wave",
                 "groups": "buttons", "source": "canonical", "hold_on_complete": False,
                 "completion_event": "ExhibitionOnboardingComplete"},
                {"type": "exhibition_onboarding_step", "step": "onboarding_ready"},
            ], "exhibition_onboarding_ready", clear_audio=True)
        if event.event_type == "AudioCompleted" and self.exhibition_mode and state.mode == "exhibition_menu_tutorial_intro":
            if str(event.payload.get("response_id") or "") != "PHRASE_016":
                return self._decision(state, [], "exhibition_menu_audio_completion_ignored")
            state.mode, state.substate = "exhibition_menu_tutorial_prompt", "awaiting_menu_prompt_audio"
            state.expected_inputs = ["audio_completed", "handset_down"]
            state.state_version += 1
            return self._decision(state, [{"type": "speak", "phrase_id": "PHRASE_007"}], "exhibition_menu_prompt_started", clear_audio=True)
        if event.event_type == "AudioCompleted" and self.exhibition_mode and state.mode == "exhibition_menu_tutorial_prompt":
            if str(event.payload.get("response_id") or "") != "PHRASE_007":
                return self._decision(state, [], "exhibition_menu_audio_completion_ignored")
            state.mode, state.substate = "exhibition_menu_tutorial_buttons", "explaining_button_1"
            state.expected_inputs = ["menu_tutorial_step", "audio_completed", "handset_down"]
            state.menu_tutorial_step = 0
            state.menu_tutorial_audio_complete = False
            state.state_version += 1
            return self._decision(state, [{"type": "speak", "phrase_id": "PHRASE_008"}], "exhibition_menu_buttons_started", clear_audio=True)
        if event.event_type == "AudioCompleted" and self.exhibition_mode and state.mode in {
            "exhibition_menu_tutorial_buttons", "exhibition_menu_replay_buttons",
        }:
            if str(event.payload.get("response_id") or "") != "PHRASE_008" or state.menu_tutorial_audio_complete:
                return self._decision(state, [], "exhibition_menu_audio_completion_ignored")
            state.menu_tutorial_audio_complete = True
            state.mode, state.substate = "exhibition_menu_wave", "awaiting_menu_wave"
            state.expected_inputs = ["menu_wave_complete", "handset_down"]
            state.state_version += 1
            return self._decision(state, [
                {"type": "lighting_wave_intent", "pattern": "menu_buttons_wave",
                 "groups": "menu_buttons", "source": "canonical", "hold_on_complete": True,
                 "completion_event": "ExhibitionMenuWaveComplete"},
                {"type": "exhibition_menu_tutorial_step", "step": "menu_wave"},
            ], "exhibition_menu_audio_completed", clear_audio=True)
        if event.event_type == "ExhibitionMenuWaveComplete" and self.exhibition_mode:
            if state.mode != "exhibition_menu_wave":
                return self._decision(state, [], "exhibition_menu_wave_completion_ignored")
            state.state_version += 1
            if state.menu_prompt_after_wave:
                state.mode, state.substate = "exhibition_menu_repeat_prompt", "awaiting_repeat_prompt_audio"
                state.expected_inputs = ["audio_completed", "handset_down"]
                state.menu_repeat_attempts = 0
                return self._decision(state, [
                    {"type": "speak", "phrase_id": "PHRASE_009"},
                    {"type": "exhibition_menu_tutorial_step", "step": "repeat_prompt"},
                ], "exhibition_menu_repeat_prompt_started")
            state.mode, state.substate = "menu_ready", "menu_tutorial_complete"
            state.expected_inputs = [f"button_{item}" for item in range(1, 8)] + ["handset_down"]
            return self._decision(state, [
                {"type": "menu_tutorial_complete", "buttons": list(state.menu_lit_buttons)},
            ], "exhibition_menu_replay_complete")
        if event.event_type == "AudioCompleted" and self.exhibition_mode and state.mode == "exhibition_menu_repeat_prompt":
            if str(event.payload.get("response_id") or "") != "PHRASE_009":
                return self._decision(state, [], "exhibition_menu_repeat_prompt_completion_ignored")
            state.mode, state.substate = "exhibition_menu_repeat_listen", "awaiting_repeat_answer"
            state.expected_inputs = ["speech", "handset_down"]
            state.menu_repeat_attempts = 0
            state.state_version += 1
            return self._decision(state, [
                {"type": "capture_open"},
                {"type": "exhibition_menu_tutorial_step", "step": "repeat_listen"},
            ], "exhibition_menu_repeat_listen_ready", clear_audio=True)
        if event.event_type == "AudioCompleted" and self.exhibition_mode and state.mode == "exhibition_menu_repeat_listen":
            if str(event.payload.get("response_id") or "") != "PHRASE_074":
                return self._decision(state, [], "exhibition_menu_repeat_retry_completion_ignored")
            state.expected_inputs = ["speech", "handset_down"]
            state.state_version += 1
            return self._decision(state, [{"type": "capture_open"}], "exhibition_menu_repeat_listen_ready", clear_audio=True)
        if event.event_type == "AudioCompleted" and state.mode == "story_pause_announcement":
            if response_id != "PHRASE_062":
                return self._decision(state, [], "story_pause_announcement_completion_ignored")
            state.mode, state.substate = "story_paused", "story_paused_ready"
            state.expected_inputs = ["button_6", "button_7", "handset_down"]
            state.state_version += 1
            return self._decision(state, [], "story_pause_announcement_completed", clear_audio=True)
        if event.event_type == "AudioCompleted" and state.mode == "story_resume_announcement":
            if response_id != "PHRASE_064":
                return self._decision(state, [], "story_resume_announcement_completion_ignored")
            if state.active_story_id == "PERSONAL_STORY":
                state.mode = "personal_story_playing"
                state.state_version += 1
                return self._decision(state, [{"type":"play_personal_story", "content_id":"PERSONAL_STORY",
                    "checkpoint":state.dialogue_memory.story_checkpoint}], "personal_story_resumed", clear_audio=True)
            state.mode, state.substate = "fixed_story_playing", "story_resumed_after_prompt"
            state.expected_inputs = ["button_6", "button_7", "handset_down"]
            state.state_version += 1
            return self._decision(state, [{"type": "story_audio_resume", "story_id": state.active_story_id, "checkpoint": state.dialogue_memory.story_checkpoint}], "story_resumed_after_prompt", clear_audio=True)
        if event.event_type == "StoryChoiceWaveComplete" and state.mode == "fixed_story_choice_wave":
            state.mode, state.substate = "fixed_story_menu", "story_choice_ready"
            state.expected_inputs = [f"button_{item}" for item in range(1, 6)] + ["button_7", "handset_down"]
            state.state_version += 1
            return self._decision(state, [{"type": "lighting_preset", "preset": "STORY_MENU"}], "story_choice_wave_complete", clear_audio=True)
        if event.event_type == "AudioCompleted" and state.mode in {"fixed_story_playing", "story_speech_analysis"}:
            if state.activity_return_mode == 'bear_dialogue':
                return self._activity_back(state, 'fixed_story_finished')
            state.mode, state.substate = "fixed_story_choice_wave", "story_finished"
            state.expected_inputs = ["story_choice_wave_complete", "button_7", "handset_down"]
            state.state_version += 1
            return self._decision(state, [
                {"type": "audio_stop"},
                {"type": "lighting_preset", "preset": "STORY_MENU"},
                {"type": "speak", "phrase_id": "PHRASE_066"},
                {"type": "lighting_wave_intent", "pattern": "story_choice_wave", "groups": "story_choice",
                 "base": "story_menu", "source": "canonical", "hold_on_complete": False,
                 "completion_event": "StoryChoiceWaveComplete", "stage_ms": 420},
            ], "fixed_story_finished", clear_audio=True)
        if event.event_type == "AudioFailed":
            if state.activity_return_mode == 'bear_dialogue' and state.mode in {
                    'fixed_story_playing','story_speech_analysis','story_pause_announcement','story_paused','story_resume_announcement'}:
                return self._activity_back(state, 'fixed_story_playback_failed')
            state.audio_failure_code = str(event.payload.get("error_code") or event.payload.get("code") or "audio_failed")[:80]
            if state.safety_severity == "urgent" or state.mode == "safety_stop":
                state.audio_status = "failed"
                state.state_version += 1
                return self._decision(state, [
                    {"type": "audio_stop"},
                    {"type": "lighting_preset", "preset": "SAFE_STOP"},
                ], "audio_failed_safety_preserved", clear_audio=True)
            state.audio_status = "failed"
            state.expected_inputs = ["handset_down", "reset"]
            state.state_version += 1
            return self._decision(state, [{"type": "audio_stop"},
                                          {"type": "content_blocked", "error_code": state.audio_failure_code},
                                          {"type": "lighting_preset", "preset": "ERROR_RECOVERABLE"}],
                                  "audio_failed_safe_stop", clear_audio=True)
        if event.event_type == "AnonymousStart":
            self.clear_identity(state)
            state.mode, state.state_version = ("session_closed" if self.optional_identity and state.handset == "up" else "idle" if self.optional_identity else "anonymous_visual"), state.state_version + 1
            effects = [{"type": "audio_stop"}, {"type": "capture_close"}, {"type": "identity_cleared"}]
            effects.append({"type": "lighting_dark"} if self.optional_identity else {"type": "lighting", "elements": [14], "state": 1})
            return self._decision(state, effects, "anonymous_started")
        if event.event_type == "HandsetPlaced":
            if self.exhibition_mode:
                # Exhibition handset-down is a hard physical-session boundary.
                # Do not enter clinical suspended RAM or preserve any dialogue.
                prior_version = state.state_version
                prior_sequences = dict(state.last_sequence_by_source)
                prior_deadline = state.welcome_deadline_monotonic
                state = self._new_state(event.session_id, None)
                state.state_version = prior_version + 1
                state.handset = "down"
                state.mode, state.substate = "idle", "exhibition_reset"
                state.expected_inputs = ["handset_up"]
                state.last_sequence_by_source = prior_sequences
                self.sessions[event.session_id] = state
                return self._decision(state, [
                    {"type": "audio_stop"},
                    {"type": "capture_close"},
                    {"type": "welcome_timer_cancelled", "deadline_monotonic": prior_deadline},
                    {"type": "exhibition_effects_cancelled"},
                    {"type": "exhibition_reset"},
                    {"type": "standby_dark_started"},
                ], "exhibition_reset", reset_session=True)
            state.handset = "down"
            if state.mode == "voice_welcome" and state.welcome_status == "pending":
                state.welcome_status = "cancelled"
                state.mode, state.expected_inputs = "idle", ["handset_up"]
                state.state_version += 1
                return self._decision(state, [
                    {"type": "audio_stop"}, {"type": "capture_close"},
                    {"type": "welcome_timer_cancelled", "deadline_monotonic": state.welcome_deadline_monotonic},
                ], "exhibition_standby")
            state.dialogue_memory.suspend(now)
            if state.mode == "game_active":
                effects = [{"type": "audio_stop"}, {"type": "capture_close"}, {"type": "game_continue_visual"}]
                if state.game_engine_state is not None and state.game_engine_state.seven_exit_phase != 'idle':
                    state.game_engine_state = cancel_exit_gesture(state.game_engine_state)
                    state.game_audio_stage = 'prompt' if state.game_engine_state.phase == 'awaiting_prompt' else 'playing'
                    effects.append(self._game_lighting(state.game_engine_state))
                if (state.substate != "game_exit_confirmation" and state.game_engine_state is not None
                        and state.game_engine_state.phase == "instruction"):
                    state.game_engine_state = begin_demonstration(state.game_engine_state, monotonic_now)
                    if getattr(state.game_engine_state, 'immersive', False):
                        state.game_audio_stage = 'playing'
                    effects.append(self._game_lighting(state.game_engine_state))
                elif (state.game_engine_state is not None
                        and state.game_engine_state.game_id == 'free_lights'):
                    state.game_audio_stage = 'playing'
            elif state.mode == "fixed_story_playing":
                state.dialogue_memory.story_checkpoint = {"story_id": state.active_story_id, "offset_ms": int(event.payload.get("offset_ms", 0))}
                state.mode = "story_suspended_ram"; effects = [{"type": "audio_stop"}, {"type": "capture_close"}]
            else:
                state.mode = "dialogue_suspended_ram"; effects = [{"type": "audio_stop"}, {"type": "capture_close"}, {"type": "clear_private_display"}]
            state.state_version += 1
            return self._decision(state, effects, "handset_down")
        if event.event_type == "HandsetLifted":
            preserve_lighting = state.mode == "profile_ready"
            if self.optional_identity and state.mode == "profile_ready":
                if (state.identity_ready_deadline is not None and monotonic_now >= state.identity_ready_deadline):
                    self.clear_identity(state)
                state.identity_ready_deadline = None

                state.mode = "idle"
            elif state.mode == "session_closed" and state.handset == "down" and state.substate == "soft_end":
                state.mode = "idle"
                state.substate = None
            if state.interaction_route == "panel":
                state.handset = "up"
                return self._session_action_noop(state, "panel_handset_lifted")
            state.interaction_route = "handset"
            state.handset = "up"; state.state_version += 1
            if state.mode == "idle":
                return self._start_exhibition_welcome(state, monotonic_now,
                    reason="exhibition_handset_lifted", preserve_lighting=preserve_lighting)
            if state.mode == "schedule_weather":
                state.expected_inputs = ["button", "speech", "handset_down"]
                return self._decision(state, [{"type": "capture_open"},
                    {"type": "typed_info_request", "kind": "menu"}], "typed_menu_after_handset")
            if state.mode == "wellbeing_check":
                state.expected_inputs = ["button_1", "button_5", "button_7", "speech", "handset_down"]
                return self._decision(state, [
                    {"type": "capture_open"}, {"type": "lighting_preset", "preset": "HELP"},
                    {"type": "support_question", "question": state.support_question_kind or "pain_or_discomfort"},
                ], "support_question_after_handset")
            if state.mode == "game_active":
                state.expected_inputs = ["button", "speech", "handset_down"]
                # Re-own a prompt interrupted by the clinical handset-down path.
                if (state.game_engine_state is not None and getattr(state.game_engine_state, 'immersive', False)
                        and state.game_audio_stage in {'starting','prompt'}):
                    return self._decision(state, [self._game_prompt(state)], 'game_prompt_rejoined')
                # Joining voice must not erase the visual game's current target.
                return self._decision(state, [{"type": "capture_open"}], "game_voice_join")
            if state.dialogue_memory.status == "suspended" and state.dialogue_memory.expires_at and now < state.dialogue_memory.expires_at:
                if state.mode == "story_suspended_ram":
                    state.mode = "story_paused"
                    state.expected_inputs = ["button", "speech", "handset_down"]
                    return self._decision(state, [{"type": "capture_open"}, {"type": "speak", "phrase_id": "PHRASE_063"}], "story_resume_offer")
                state.mode = "awaiting_handset"
                state.expected_inputs = ["button", "speech", "handset_down"]
                return self._decision(state, [{"type": "capture_open"}, {"type": "speak", "phrase_id": "PHRASE_002"}], "resume_offer")
            if state.pending_mode == "fixed_story_playing" and state.active_story_id:
                story_id = state.active_story_id
                verified = self.stories.get_verified(story_id) if self.stories else None
                placeholder = self.stories.get_development_placeholder() if self.stories and self.allow_development_placeholders else None
                state.pending_mode = None
                state.mode = "fixed_story_playing"
                state.expected_inputs = ["button_6", "button_7", "handset_down"]
                story_wave = {"type": "lighting_wave_intent", "pattern": "story_playback_wave", "groups": "story_playback", "source": "canonical", "loop": True, "stage_ms": 620}
                if verified is not None:
                    return self._decision(state, [story_wave, {"type": "play_fixed_story", "story_id": story_id, "asset": verified["audio_file"], "duration_ms": verified["duration_ms"]}], "fixed_story_started_after_handset")
                if placeholder is not None:
                    return self._decision(state, [story_wave, {"type": "play_placeholder", "content_kind": "fixed_story", "story_id": story_id, "asset": placeholder["audio_file"], "development_only": True}], "fixed_story_placeholder_after_handset")
            if state.mode in {"game_menu", "fixed_story_menu", "support_intake", "personal_story_theme", "wellbeing_check", "bear_dialogue", "schedule_weather"}:
                state.expected_inputs = ["button", "speech", "handset_down"]
                # The active menu owns its button affordances; microphone admission
                # is not a navigation event and must preserve that lighting.
                return self._decision(state, [{"type": "capture_open"}], "voice_open_activity")
            if state.profile_status == "identified":
                state.mode = "root_menu"
                state.expected_inputs = ["button", "speech", "handset_down"]
                greeting = f"Привет, {state.display_name}. Я Мишка" if state.display_name else "Привет. Я Мишка"
                return self._decision(state, [{"type": "speak_dynamic", "text": greeting, "personalized": bool(state.display_name)}, {"type": "lighting_preset", "preset": "ROOT_MENU"}], "identified_menu_opened")
            if state.pending_mode:
                pending = state.pending_mode
                state.pending_mode = None
                state.mode = pending
                state.expected_inputs = ["button", "speech", "handset_down"]
                return self._decision(state, [{"type": "capture_open"}, {"type": "lighting_preset", "preset": "LISTENING"}], "voice_open_pending_activity")
            state.mode = "voice_welcome"
            state.expected_inputs = ["button", "speech", "handset_down"]
            return self._decision(state, [{"type": "capture_open"}, {"type": "lighting_preset", "preset": "LISTENING"}], "voice_open")
        if (event.event_type.startswith("Button") and state.mode == "game_active"
                and state.substate == "game_exit_confirmation" and state.game_engine_state is not None
                and int(event.payload["button_id"]) in (1, 2, 3, 4)):
            state.game_engine_state = advance_game(state.game_engine_state, edge=event.event_type,
                button=int(event.payload["button_id"]), sequence=state.game_engine_state.last_sequence + 1, now=monotonic_now, paused=True).state
            return self._decision(state, [], "game_exit_confirmation_pending")
        if (event.event_type in {"ButtonReleased", "ButtonHeld", "ButtonLongHeld"}
                and state.mode == "game_active" and state.substate != "game_exit_confirmation"):
            return self._game_input(state, event, monotonic_now)
        if event.event_type in {"ButtonPressed","ButtonReleased","ButtonHeld","ButtonLongHeld"}:
            menu_result = self._scene_menu_button(state,event)
            if menu_result is not None: return menu_result
        if event.event_type == "ButtonReleased" and state.story_menu_skip_button == int(event.payload["button_id"]):
            state.story_menu_skip_button = None
            return self._decision(state, [], "story_menu_hint_skip_released")
        if event.event_type == "ButtonReleased" and self.exhibition_mode and state.mode == "menu_ready":
            button = int(event.payload["button_id"])
            if button not in state.menu_pressed_buttons:
                return self._decision(state, [], "exhibition_menu_button_release_ignored")
            state.menu_pressed_buttons.remove(button)
            state.state_version += 1
            return self._decision(state, [], "exhibition_menu_button_released")
        if event.event_type == "ButtonPressed":
            button = int(event.payload["button_id"])
            if state.mode == "bear_dialogue" and state.pending_game_offer:
                if button == 7:
                    state.pending_game_offer = state.game_offer_id = None
                    state.game_offer_ready = False
                    state.expected_inputs = ["speech", "button_7", "handset_down"]
                    state.state_version += 1
                    return self._decision(state, [{"type": "audio_stop"}, {"type": "capture_open"},
                        {"type": "lighting_preset", "preset": "LISTENING"}], "game_offer_declined", clear_audio=True)
                if button == 1 and fresh_button_press and state.game_offer_ready and not state.active_audio_id:
                    return self._start_game(state, state.pending_game_offer, monotonic_now, button, from_dialogue=True)
                return self._decision(state, [], "game_offer_prompt_waiting")
            if self.exhibition_mode and state.mode in {"idle", "voice_welcome"}:
                return self._decision(state, [], "exhibition_age_not_ready")
            if state.story_menu_skip_button == button:
                return self._decision(state, [], "story_menu_hint_skip_held")
            if state.mode == "fixed_story_menu" and state.story_menu_hint_active and button != 5:
                state.story_menu_hint_active = False
                state.story_menu_skip_button = button
                state.state_version += 1
                return self._decision(state, [{"type": "audio_stop"}], "story_menu_hint_skipped", clear_audio=True)
            if self.exhibition_mode and state.mode in {"exhibition_name_capture", "exhibition_age_prompt", "exhibition_age_hint", "exhibition_onboarding_complete", "exhibition_age_complete"}:
                return self._decision(state, [], "exhibition_onboarding_button_ignored")
            if self.exhibition_mode and state.mode in {
                "exhibition_menu_tutorial_intro", "exhibition_menu_tutorial_prompt",
                "exhibition_menu_tutorial_buttons", "exhibition_menu_wave",
                "exhibition_menu_repeat_prompt", "exhibition_menu_repeat_listen",
                "exhibition_menu_replay_buttons",
            }:
                # A physical button press is an explicit child-friendly skip
                # gesture.  Do not also activate the selected menu action on
                # this same edge; the next press starts a clean menu action.
                state.mode, state.substate = "menu_ready", "menu_tutorial_skipped"
                state.expected_inputs = [f"button_{item}" for item in range(1, 8)] + ["handset_down"]
                state.menu_lit_buttons = list(range(1, 8))
                # The skip edge stays consumed until physical release. A
                # repeated key-down must not also activate the menu action.
                state.menu_pressed_buttons = [button]
                state.menu_tutorial_phase = "skipped"
                state.menu_repeat_attempts = 0
                state.state_version += 1
                return self._decision(state, [
                    {"type": "audio_stop"},
                    {"type": "capture_close"},
                    {"type": "exhibition_menu_tutorial_skip", "button": button},
                    {"type": "exhibition_effects_cancelled"},
                    {"type": "lighting_preset", "preset": "ROOT_MENU"},
                    {"type": "menu_tutorial_complete", "buttons": list(state.menu_lit_buttons), "skipped": True},
                ], "exhibition_menu_tutorial_skipped", clear_audio=True)
            if self.exhibition_mode and state.mode == "menu_ready":
                if button in state.menu_pressed_buttons:
                    return self._decision(state, [], "exhibition_menu_button_duplicate")
                action = {
                    1: "games", 2: "stories", 3: "tell_or_ask", 4: "new_story",
                    5: "help", 6: "talk", 7: "day_and_plans",
                }[button]
                state.menu_pressed_buttons.append(button)
                state.selected_menu_button = button
                state.selected_menu_action = action
                state.state_version += 1
                selection = {
                    "type": "exhibition_menu_selected", "event_type": "ExhibitionMenuSelected",
                    "session_id": state.session_id, "button_number": button, "action": action,
                    "source": "exhibition_menu", "target_state": "menu_ready",
                    "downstream_owner": "exhibition_menu_boundary",
                }
                if button in {1, 3, 4, 5, 7}:
                    mode = BUTTON_MODES[button]
                    state.menu_pressed_buttons = []
                    if button == 3 and not state.interaction_open:
                        state.pending_mode = mode
                        state.mode = "awaiting_handset"
                        selection["target_state"] = "awaiting_handset"
                        selection["downstream_owner"] = mode
                        state.expected_inputs = ["handset_up", "handset_down"]
                        return self._decision(state, [
                            selection,
                            {"type": "lighting", "elements": [14], "state": 1},
                            {"type": "visual", "view": "lift_handset"},
                        ], "handset_required")
                    state.mode = mode
                    state.expected_inputs = (
                        ["button", "speech", "handset_down"] if state.interaction_open
                        else ["button", "handset_down"]
                    )
                    selection["target_state"] = mode
                    selection["downstream_owner"] = mode
                    preset = {1: "GAME_MENU", 3: "SUPPORT", 4: "PERSONAL_STORY",
                              5: "HELP", 7: "SCHEDULE_WEATHER"}[button]
                    entry_effects = [selection, {"type": "lighting_preset", "preset": preset}]
                    if button == 7:
                        entry_effects.append({"type": "typed_info_request", "kind": "menu"})
                    if button == 5:
                        state.support_question_kind = "pain_or_discomfort"
                        state.expected_inputs = ["button_1", "button_5", "button_7", "speech", "handset_down"]
                        entry_effects.append({"type": "support_question", "question": state.support_question_kind})
                    return self._decision(state, entry_effects, "button_mode")
                if button == 2:
                    state.mode, state.substate = "fixed_story_menu", "story_menu_opened"
                    state.menu_pressed_buttons = []
                    state.expected_inputs = [f"button_{item}" for item in range(1, 6)] + ["button_7", "handset_down"]
                    selection["target_state"] = "fixed_story_menu"
                    selection["downstream_owner"] = "fixed_story_menu"
                    return self._decision(state, [
                        selection,
                        {"type": "lighting_preset", "preset": "STORY_MENU"},
                        {"type": "speak", "phrase_id": "PHRASE_054"},
                    ], "fixed_story_menu_opened", clear_audio=True)
                if button == 6:
                    state.mode, state.substate = "bear_dialogue", "dialogue_started"
                    state.expected_inputs = ["speech", "button_7", "handset_down"]
                    state.dialogue_memory.touch(now)
                    selection["target_state"] = "bear_dialogue"
                    selection["downstream_owner"] = "bear_dialogue"
                    return self._decision(state, [
                        selection,
                        {"type": "lighting_preset", "preset": "BEAR_DIALOGUE"},
                        {"type": "capture_open"},
                        {"type": "speak", "phrase_id": "PHRASE_094"},
                    ], "bear_dialogue_started", clear_audio=True)
            if state.mode == "session_closed":
                return self._decision(state, [], "session_closed")
            if button == 5 and state.mode in {
                "idle", "visual_welcome", "anonymous_visual", "root_menu",
                "dialogue_memory_expired", "dialogue_suspended_ram", "voice_welcome",
                "awaiting_handset", "wellbeing_check", "personal_welcome_listen",
                "game_active", "fixed_story_playing", "story_paused", "fixed_story_menu",
                "game_menu", "support_intake", "personal_story_theme", "bear_dialogue",
                "schedule_weather", "personal_story_generating",
                "personal_story_validating", "personal_story_playing", "story_pause_announcement", "story_resume_announcement",
            } and state.mode != "game_menu" and not (
                state.mode == "game_active" and state.game_engine_state is not None
                and getattr(state.game_engine_state, 'immersive', False)
                and button in game_answer_buttons(state.game_engine_state)
            ) and not (self.exhibition_mode and state.mode == "exhibition_age_selection"):
                question = state.support_question_kind if state.mode == "wellbeing_check" else None
                state.support_question_kind = question or "pain_or_discomfort"
                state.mode, state.expected_inputs, state.state_version = "wellbeing_check", ["button_1", "button_5", "button_7", "speech", "handset_down"], state.state_version + 1
                return self._decision(state, [{"type": "audio_stop"}, {"type": "lighting_preset", "preset": "HELP"}, {"type": "support_question", "question": state.support_question_kind}], "help_priority")
            if state.mode == "wellbeing_check" and not state.interaction_open:
                state.expected_inputs = ["handset_up", "button_5"]
                return self._decision(state, [{"type": "visual", "view": "lift_handset"}], "support_handset_required")
            if state.mode == "personal_welcome_listen":
                if state.audio_status == "failed":
                    return self._decision(state, [], "personal_welcome_audio_failed")
                if button == 7:
                    return self._personal_welcome_menu(state, "personal_welcome_menu_escape")
                return self._decision(state, [], "personal_welcome_waiting")
            if self.exhibition_mode and state.mode == "exhibition_age_selection":
                if button not in range(1, 8):
                    return self._decision(state, [], "exhibition_age_button_invalid")
                return self._select_exhibition_age(state, button, source="exhibition_button", now=now)
            if state.mode == "automatic_mood_check":
                mood = "low" if button in {1, 2, 3} else "calm" if button in {4, 5} else "good"
                state.mood_result = mood
                state.mode, state.state_version = "root_menu", state.state_version + 1
                return self._decision(state, [{"type": "mood_report", "value": mood, "input": f"button_{button}"}, {"type": "speak", "phrase_id": "PHRASE_017"}, {"type": "lighting_preset", "preset": "ROOT_MENU"}], "mood_report")
            if (button == 7 and (state.mode == "personal_story_playing" or
                    (state.active_story_id == "PERSONAL_STORY" and state.mode in {
                        "story_pause_announcement", "story_paused", "story_resume_announcement"}))):
                state.mode = "root_menu"
                state.active_story_id = None
                state.state_version += 1
                return self._decision(state, [{"type":"audio_stop"},{"type":"lighting_preset","preset":"ROOT_MENU"}], "personal_story_back", clear_audio=True)
            if state.mode in {"fixed_story_playing", "story_speech_analysis", "personal_story_playing"}:
                if button == 6:
                    state.mode, state.substate = "story_pause_announcement", "story_pause_requested"
                    state.state_version += 1
                    state.dialogue_memory.story_checkpoint = {"story_id": state.active_story_id, "offset_ms": int(event.payload.get("offset_ms", 0))}
                    state.story_checkpoint_confirmed = False
                    state.expected_inputs = ["audio_completed", "button_7", "handset_down"]
                    return self._decision(state, [
                        {"type": "story_audio_pause", "story_id": state.active_story_id},
                        {"type": "lighting_wave_pause"},
                        {"type": "lighting_story_pause_flash"},
                        {"type": "speak", "phrase_id": "PHRASE_062"},
                    ], "story_pause_requested", clear_audio=True)
                if button == 7:
                    if state.active_story_id != 'PERSONAL_STORY' and state.activity_return_mode == 'bear_dialogue':
                        return self._activity_back(state, 'story_stopped_by_button')
                    state.mode, state.substate = "fixed_story_choice_wave", "story_stop_requested"
                    state.state_version += 1
                    state.expected_inputs = ["story_choice_wave_complete", "handset_down"]
                    return self._decision(state, [
                        {"type": "audio_stop"},
                        {"type": "lighting_preset", "preset": "STORY_MENU"},
                        {"type": "speak", "phrase_id": "PHRASE_066"},
                        {"type": "lighting_wave_intent", "pattern": "story_choice_wave", "groups": "story_choice",
                         "base": "story_menu", "source": "canonical", "hold_on_complete": False,
                         "completion_event": "StoryChoiceWaveComplete", "stage_ms": 420},
                    ], "story_stopped_by_button", clear_audio=True)
                return self._decision(state, [{"type": "invalid_input", "button_id": button}], "invalid_story_input")
            if state.mode in {"story_pause_announcement", "story_resume_announcement", "story_paused"} and button == 7:
                if state.activity_return_mode == 'bear_dialogue':
                    return self._activity_back(state, 'story_stopped_by_button')
                state.mode, state.substate = "fixed_story_choice_wave", "story_stop_requested"
                state.state_version += 1
                state.expected_inputs = ["story_choice_wave_complete", "handset_down"]
                return self._decision(state, [
                    {"type": "audio_stop"},
                    {"type": "lighting_preset", "preset": "STORY_MENU"},
                    {"type": "speak", "phrase_id": "PHRASE_066"},
                    {"type": "lighting_wave_intent", "pattern": "story_choice_wave", "groups": "story_choice",
                     "base": "story_menu", "source": "canonical", "hold_on_complete": False,
                     "completion_event": "StoryChoiceWaveComplete", "stage_ms": 420},
                ], "story_stopped_by_button", clear_audio=True)
            if state.mode == "fixed_story_menu" and button == 7:
                if state.activity_return_mode == 'bear_dialogue':
                    return self._activity_back(state, 'story_menu_back')
                state.mode, state.state_version = "root_menu", state.state_version + 1
                return self._decision(state, [{"type": "lighting_preset", "preset": "ROOT_MENU"}], "story_menu_back")
            if state.mode == "game_menu" and button == 7:
                if state.activity_return_mode == 'bear_dialogue':
                    return self._activity_back(state, 'game_menu_back')
                state.mode, state.state_version = "root_menu", state.state_version + 1
                return self._decision(state, [{"type": "lighting_preset", "preset": "ROOT_MENU"}], "game_menu_back")
            if state.mode in {"support_intake", "bear_dialogue", "personal_story_generating",
                              "personal_story_validating", "personal_story_playing"} and button == 7:
                state.mode, state.state_version = "root_menu", state.state_version + 1
                return self._decision(state, [{"type": "audio_stop"}, {"type": "capture_close"}, {"type": "lighting_preset", "preset": "ROOT_MENU"}], "activity_back")
            if state.mode == "wellbeing_check" and button == 5:
                return self._decision(state, [{"type": "support_question", "question": "pain_or_discomfort"}], "wellbeing_repeat")
            if state.mode == "wellbeing_check" and button == 7:
                return self._enter_local_safety(state, severity="concern", reason="help_requested")
            if state.mode == "wellbeing_check" and button == 1:
                return self._support_no(state)
            if state.mode in {"idle", "visual_welcome", "anonymous_visual", "root_menu", "dialogue_memory_expired", "dialogue_suspended_ram", "voice_welcome", "automatic_mood_check", "awaiting_handset"}:
                mode = BUTTON_MODES[button]
                if mode in {"game_menu", "fixed_story_menu", "personal_story_theme", "schedule_weather"}:
                    state.scene_menu_held = [button]
                if button in {3, 6} and not state.interaction_open:
                    state.pending_mode = mode
                    state.mode, state.state_version = "awaiting_handset", state.state_version + 1
                    return self._decision(state, [{"type": "lighting", "elements": [14], "state": 1}, {"type": "visual", "view": "lift_handset"}], "handset_required")
                state.mode, state.state_version = mode, state.state_version + 1
                state.expected_inputs = (["speech", "button_7", "handset_down"]
                                         if mode == "bear_dialogue" and state.interaction_open
                                         else ["button", "speech"] if state.interaction_open else ["button"])
                preset = {
                    "game_menu": "GAME_MENU", "fixed_story_menu": "STORY_MENU",
                    "support_intake": "SUPPORT", "personal_story_theme": "PERSONAL_STORY",
                    "wellbeing_check": "HELP", "bear_dialogue": "BEAR_DIALOGUE",
                    "schedule_weather": "SCHEDULE_WEATHER",
                }.get(mode, "ROOT_MENU")
                effects = [{"type": "lighting_preset", "preset": preset}]
                if mode == "schedule_weather":
                    effects.append({"type": "typed_info_request", "kind": "menu"})
                if mode == "bear_dialogue" and state.interaction_open:
                    state.substate = "dialogue_started"
                    state.dialogue_memory.touch(now)
                    effects.extend([
                        {"type": "capture_open"},
                        {"type": "speak", "phrase_id": "PHRASE_094"},
                    ])
                    return self._decision(state, effects, "bear_dialogue_started", clear_audio=True)
                return self._decision(state, effects, "button_mode")
            if state.mode == "personal_story_theme":
                theme = self._menu_selection(state, button)
                if button == 7:
                    state.mode, state.state_version = "root_menu", state.state_version + 1
                    return self._decision(state, [{"type": "lighting_preset", "preset": "ROOT_MENU"}], "personal_story_cancelled")
                if theme is not None:
                    state.mode, state.state_version = "personal_story_generating", state.state_version + 1
                    brief = {"age_band": state.age_band, "theme": theme, "forbidden_topics": ["diagnosis", "treatment", "real_schedule", "secrets"], "content_constraints": list(state.accessibility_policy.get("content_constraints", ()))}
                    return self._decision(state, [{"type": "generate_personal_story", "brief": brief}], "personal_story_requested_by_button")
            if state.mode == "story_paused" and button == 6:
                if state.public_story_checkpoint_pending:
                    return self._decision(state, [], "public_story_checkpoint_pending")
                state.mode, state.substate, state.state_version = "story_resume_announcement", "story_resume_requested", state.state_version + 1
                state.expected_inputs = ["audio_completed", "button_7", "handset_down"]
                return self._decision(state, [{"type": "lighting_wave_resume"}, {"type": "speak", "phrase_id": "PHRASE_064"}], "story_resume_requested", clear_audio=True)
            if state.mode == "game_menu":
                game_id = self._menu_selection(state, button)
                return self._start_game(state, game_id, monotonic_now, button)
            if (state.mode == "game_active" and state.game_engine_state is not None
                    and getattr(state.game_engine_state, 'immersive', False) and button == 7
                    and not hold_exit_policy(state.game_engine_state)):
                return self._game_exit(state, 'game_exited_by_button')
            if state.mode == "game_active" and state.substate == "game_exit_confirmation":
                if button == 7:
                    state.mode, state.substate, state.state_version = "root_menu", "game_exit_confirmed", state.state_version + 1
                    return self._decision(state, [{"type": "audio_stop"}, {"type": "lighting_preset", "preset": "ROOT_MENU"}], "game_exit_confirmed")
                if button == 6:
                    if state.game_engine_state is not None:
                        state.game_engine_state = arm_wait(state.game_engine_state, monotonic_now)
                    state.substate, state.state_version = "game_active", state.state_version + 1
                    return self._decision(state, [{"type": "audio_stop"}, self._game_lighting(state.game_engine_state)], "game_exit_cancelled", clear_audio=True)
                # Other game buttons cannot score or dismiss a pending exit.
                # The earlier global help/safety path keeps its priority.
                return self._decision(state, [], "game_exit_confirmation_pending")
            if (state.mode == "game_active" and button == 7
                    and not (state.game_engine_state is not None
                             and getattr(state.game_engine_state, 'immersive', False))):
                if state.game_engine_state is not None:
                    state.game_engine_state = pause_game(state.game_engine_state)
                state.substate, state.state_version = "game_exit_confirmation", state.state_version + 1
                effects = [{"type": "audio_stop"},
                           {"type": "lighting_preset", "preset": "PAUSED"},
                           {"type": "game_exit_confirmation", "requires_explicit_choice": True}]
                if state.interaction_open:
                    effects.append({"type": "speak", "phrase_id": "PHRASE_062"})
                return self._decision(state, effects, "game_exit_confirmation", clear_audio=True)
            if state.mode == "game_active":
                return self._game_input(state, event, monotonic_now)
            if state.mode == "fixed_story_menu":
                story_id = self._menu_selection(state, button)
                if story_id is None:
                    return self._decision(state, [], "story_menu_ignored")
                if self.stories and self.stories.get_verified(story_id):
                    return self._start_fixed_story(state, story_id)
                verified = self.stories.get_verified(story_id) if self.stories else None
                placeholder = self.stories.get_development_placeholder() if self.stories and self.allow_development_placeholders else None
                if verified is None and placeholder is None:
                    return self._decision(state, [{"type": "content_blocked", "story_id": story_id}, {"type": "lighting_preset", "preset": "ERROR_RECOVERABLE"}], "fixed_story_blocked")
                state.active_story_id, state.state_version = story_id, state.state_version + 1
                if not state.interaction_open:
                    state.pending_mode = "fixed_story_playing"
                    state.mode = "awaiting_handset"
                    return self._decision(state, [{"type": "lighting", "elements": [14], "state": 1}, {"type": "visual", "view": "lift_handset"}], "story_handset_required")
                state.mode, state.substate = "fixed_story_playing", "story_started"
                state.expected_inputs = ["button_6", "button_7", "handset_down"]
                story_wave = {"type": "lighting_wave_intent", "pattern": "story_playback_wave", "groups": "story_playback", "source": "canonical", "loop": True, "stage_ms": 620}
                if verified is not None:
                    return self._decision(state, [story_wave, {"type": "play_fixed_story", "story_id": story_id, "asset": verified["audio_file"], "duration_ms": verified["duration_ms"]}], "fixed_story_started")
                return self._decision(state, [story_wave, {"type": "play_placeholder", "content_kind": "fixed_story", "story_id": story_id, "asset": placeholder["audio_file"], "development_only": True}], "fixed_story_placeholder")
            if state.mode == "schedule_weather":
                action = "back" if button==7 else self._menu_selection(state,button)
                if action == "back":
                    state.mode, state.state_version = "root_menu", state.state_version + 1
                    return self._decision(state, [{"type": "audio_stop"}, {"type": "capture_close"},
                        {"type": "lighting_preset", "preset": "ROOT_MENU"}], "info_back", clear_audio=True)
                if action in {"what_next", "where_now", 'card_doctors'} and state.profile_status != "identified":
                    return self._decision(state, [{"type": "privacy_blocked"}, {"type": "speak", "phrase_id": "PHRASE_101"}], "schedule_requires_profile")
                if action:
                    state.state_version += 1
                    return self._decision(state, [self._information_effect(state, action)], "typed_info_requested")
        if event.event_type == "VoiceIntentReady":
            severity = str(event.payload.get("safety_severity", "none"))
            if severity not in {"none", "concern", "urgent"}:
                return self._decision(state, [], "invalid_safety_severity")
            if severity == "urgent":
                return self._enter_local_safety(state, severity="urgent", reason="urgent_safety")
            intent = str(event.payload.get("intent", "free_speech"))
            if intent == "input_retry":
                if state.mode == "personal_welcome_listen" and state.audio_status == "failed":
                    return self._decision(state, [], "personal_welcome_audio_failed")
                # A failed/uncertain classifier cannot authorize a command or
                # identity capture, but it is not an adult-help request either.
                # Preserve current safety classification and approved story playback.
                retry_modes = {
                    "exhibition_name_capture", "exhibition_age_selection",
                    "exhibition_menu_repeat_listen", "bear_dialogue",
                    "wellbeing_check", "personal_story_theme", "automatic_mood_check", "personal_welcome_listen",
                }
                retry_effects = ([{"type": "speak", "phrase_id": "PHRASE_074"}]
                    if state.interaction_open and state.mode in retry_modes else [])
                prior_substate = state.substate
                result = self._decision(state, retry_effects, "input_safety_retry")
                state.substate = prior_substate
                result["state"]["substate"] = prior_substate
                return result
            if intent == "help_request" or (state.mode == "wellbeing_check" and intent == "yes"):
                return self._enter_local_safety(state, severity="concern", reason="help_requested")
            if severity == "concern":
                # A disclosure is never consumed as a name, age, story theme
                # or ordinary scene command, even during onboarding.
                state.mode, state.safety_severity = "support_intake", "concern"
                state.pending_tool = state.pending_mode = None
                state.support_question_kind = None
                state.active_story_id = state.active_game_id = None
                state.game_target = None
                state.game_parameters = {}
                state.dialogue_memory.story_checkpoint = None
                state.story_menu_hint_active = False
                state.story_menu_skip_button = None
                state.welcome_status = "cancelled"
                state.welcome_t0_monotonic = state.welcome_deadline_monotonic = None
                state.expected_inputs = (["speech", "button_5", "button_7", "handset_down"]
                                         if state.interaction_open else ["handset_up", "button_5"])
                state.state_version += 1
                return self._decision(state, [
                    {"type": "audio_stop"},
                    {"type": "lighting_preset", "preset": "SUPPORT"},
                    {"type": "support_response", "kind": "concern"},
                ], "concern_support", clear_audio=True)
            if state.mode == "wellbeing_check" and intent == "no":
                return self._support_no(state)
            if (self.exhibition_mode and state.mode in {"exhibition_name_capture", "exhibition_age_selection"}
                    and onboarding_declined(transient_text or "")):
                # No fabricated slot, no repeated interrogation, no new identity.
                return self._personal_welcome_menu(state, "exhibition_onboarding_declined")
            if not state.interaction_open:
                return self._decision(state, [{"type": "visual", "view": "lift_handset"}], "voice_requires_handset")
            if state.mode == "wellbeing_check" and not state.interaction_open:
                state.expected_inputs = ["handset_up", "button_5"]
                return self._decision(state, [{"type": "visual", "view": "lift_handset"}], "support_handset_required")
            if state.mode == "bear_dialogue" and state.pending_game_offer:
                if not state.game_offer_ready or not game_confirmation_authorized:
                    return self._decision(state, [], "game_offer_prompt_waiting")
                if intent == "yes":
                    return self._start_game(state, state.pending_game_offer, monotonic_now, from_dialogue=True)
                state.pending_game_offer = state.game_offer_id = None
                state.game_offer_ready = False
                if intent == "no":
                    state.state_version += 1
                    return self._decision(state, [{"type": "audio_stop"}, {"type": "capture_open"},
                        {"type": "lighting_preset", "preset": "LISTENING"}], "game_offer_declined", clear_audio=True)
            if state.mode in {"game_active", "game_menu"} and severity == "none":
                text = " ".join(str(transient_text or "").casefold().split()).strip(" .!?…")
                leaving = (intent in {'stop', 'stop_game', 'exit_game', 'return_to_menu'}
                    or bool(re.search(r"\b(?:(?:не хочу|не буду)(?: больше)? (?:играть|игру)|стоп(?: игра| играть)?|хватит(?: играть)?|"
                                      r"закончим(?: игру| играть)?|закончить игру|останови игру|выйди из игры|выйти(?: из игры)?|"
                                      r"давай (?:закончим|в меню)|вернись в меню|надоело играть)\b", text)))
                if leaving:
                    return self._game_exit(state, 'game_exited_by_voice')
                if (state.mode == 'game_active' and state.active_game_id == 'word_game'
                        and state.game_engine_state is not None
                        and state.game_engine_state.game_id == 'word_game'):
                    if state.game_audio_stage != 'playing' or state.active_audio_id:
                        return self._decision(state, [], 'word_game_prompt_waiting')
                    state.game_engine_state = pause_game(state.game_engine_state)
                    state.dialogue_memory.touch(now)
                    state.state_version += 1
                    return self._decision(state, [{'type':'bounded_dialogue_turn',
                        'intent':intent, 'game_id':'word_game', 'age_band':state.age_band}], 'word_game_turn')
                if state.mode == 'game_active':
                    return self._decision(state, [], 'game_voice_ignored')
            if state.mode == "personal_welcome_listen":
                if state.audio_status == "failed" or state.active_audio_id or not personal_welcome_answer_verified:
                    return self._decision(state, [], "personal_welcome_waiting")
                return self._personal_welcome_menu(state, "personal_welcome_answer_received")
            if severity == "none":
                menu_result = self._scene_menu_answer(state,intent,transient_text,personal_welcome_answer_verified,catalogue_descriptor)
                if menu_result is not None: return menu_result
            if self.exhibition_mode and state.mode == "exhibition_menu_repeat_listen":
                repeat_answer = normalize_exhibition_repeat_answer(transient_text or "") or (intent if intent in {"yes", "no"} else None)
                if repeat_answer == "yes":
                    state.mode, state.substate = "exhibition_menu_replay_buttons", "awaiting_menu_audio"
                    state.expected_inputs = ["audio_completed", "handset_down"]
                    state.menu_tutorial_step = 0
                    state.menu_tutorial_phase = "idle"
                    state.menu_tutorial_audio_complete = False
                    state.menu_prompt_after_wave = False
                    state.menu_lit_buttons = []
                    state.menu_repeat_attempts = 0
                    state.state_version += 1
                    return self._decision(state, [
                        {"type": "capture_close"},
                        {"type": "speak", "phrase_id": "PHRASE_008"},
                        {"type": "exhibition_menu_tutorial_step", "step": "menu_replay"},
                    ], "exhibition_menu_replay_started", clear_audio=True)
                if repeat_answer == "no":
                    state.mode, state.substate = "menu_ready", "menu_repeat_declined"
                    state.expected_inputs = [f"button_{item}" for item in range(1, 8)] + ["handset_down"]
                    state.state_version += 1
                    state.menu_repeat_attempts = 0
                    return self._decision(state, [
                        {"type": "capture_close"},
                        {"type": "menu_tutorial_complete", "buttons": list(state.menu_lit_buttons)},
                    ], "exhibition_menu_repeat_declined", clear_audio=True)
                state.menu_repeat_attempts += 1
                if state.menu_repeat_attempts >= 2:
                    state.mode, state.substate = "menu_ready", "menu_repeat_defaulted"
                    state.expected_inputs = [f"button_{item}" for item in range(1, 8)] + ["handset_down"]
                    state.state_version += 1
                    return self._decision(state, [
                        {"type": "capture_close"},
                        {"type": "menu_tutorial_complete", "buttons": list(state.menu_lit_buttons), "repeat_defaulted": True},
                        {"type": "lighting_preset", "preset": "ROOT_MENU"},
                    ], "exhibition_menu_repeat_defaulted", clear_audio=True)
                state.expected_inputs = ["audio_completed", "handset_down"]
                state.state_version += 1
                return self._decision(state, [
                    {"type": "speak", "phrase_id": "PHRASE_074"},
                ], "exhibition_menu_repeat_unclear", clear_audio=True)
            if self.exhibition_mode and state.mode == "exhibition_age_selection":
                age_years = normalize_exhibition_age(transient_text or "")
                if age_years is None:
                    state.state_version += 1
                    return self._decision(state, [
                        {"type": "speak", "phrase_id": "PHRASE_074"},
                        {"type": "capture_open"},
                    ], "exhibition_age_unrecognized", clear_audio=True)
                return self._select_exhibition_age(state, age_years, source="exhibition_voice", now=now)
            if state.mode in {"fixed_story_playing", "story_speech_analysis"} and intent in {"pause", "pause_story"}:
                state.mode, state.substate = "story_pause_announcement", "story_pause_requested"
                state.state_version += 1
                state.dialogue_memory.story_checkpoint = {"story_id": state.active_story_id, "offset_ms": int(event.payload.get("offset_ms", 0))}
                state.story_checkpoint_confirmed = False
                return self._decision(state, [{"type": "story_audio_pause", "story_id": state.active_story_id}, {"type": "lighting_wave_pause"}, {"type": "lighting_story_pause_flash"}, {"type": "speak", "phrase_id": "PHRASE_062"}], "story_pause_requested", clear_audio=True)
            if state.mode in {"fixed_story_playing", "story_speech_analysis"} and intent in {"stop", "stop_story", "exit_story"}:
                if state.activity_return_mode == 'bear_dialogue':
                    return self._activity_back(state, 'story_stopped')
                state.mode, state.substate = "fixed_story_choice_wave", "story_stop_requested"
                state.state_version += 1
                return self._decision(state, [{"type": "audio_stop"}, {"type": "lighting_preset", "preset": "STORY_MENU"}, {"type": "speak", "phrase_id": "PHRASE_066"}, {"type": "lighting_wave_intent", "pattern": "story_choice_wave", "groups": "story_choice", "base": "story_menu", "hold_on_complete": False, "completion_event": "StoryChoiceWaveComplete", "stage_ms": 420}], "story_stopped", clear_audio=True)
            if state.mode in {"fixed_story_playing", "story_speech_analysis"} and intent == "emotional_reaction":
                return self._decision(state, [{"type": "story_speech_analysis", "playback_continues": True, "intent": "emotional_reaction"}], "story_reaction_ignored")
            if state.mode in {"fixed_story_playing", "story_speech_analysis"}:
                # Low-confidence or unknown speech is analysed without taking the playback lease.
                state.mode, state.state_version = "story_speech_analysis", state.state_version + 1
                return self._decision(state, [{"type": "story_speech_analysis", "playback_continues": True}], "story_speech_analysis")
            if state.mode == "automatic_mood_check" and intent in {"mood", "free_speech"}:
                state.mode, state.state_version = "root_menu", state.state_version + 1
                return self._decision(state, [{"type": "mood_report", "value": intent}, {"type": "speak", "phrase_id": "PHRASE_017"}], "mood_report")
            if state.mode in {"personal_story_generating", "personal_story_validating"} and intent in {"generated","validated","validation_failed"}:
                return self._decision(state, [], "personal_story_result_untrusted")
            if self.exhibition_mode and state.mode == "exhibition_name_capture":
                name = normalize_exhibition_name(transient_text or "")
                if name is None:
                    state.state_version += 1
                    return self._decision(state, [
                        {"type": "speak", "phrase_id": "PHRASE_074"},
                        {"type": "capture_open"},
                        {"type": "exhibition_name_rejected"},
                    ], "exhibition_name_unrecognized")
                if state.profile_status != "identified":
                    state.display_name = name
                state.onboarding_name_collected = True
                state.dialogue_memory.bounded_turns.append({"kind": "name", "value": name})
                state.dialogue_memory.touch(now)
                state.mode, state.substate = "exhibition_age_prompt", "awaiting_age_question_audio"
                state.expected_inputs = ["audio_completed", "handset_down"]
                state.state_version += 1
                return self._decision(state, [
                    {"type": "capture_close"},
                    {"type": "speak", "phrase_id": "PHRASE_108"},
                    {"type": "exhibition_onboarding_step", "step": "age_question"},
                ], "exhibition_age_question_started", clear_audio=True)
            if state.mode in {"support_intake", "wellbeing_check"}:
                state.state_version += 1
                if state.mode == "support_intake" and state.safety_severity == "none":
                    state.dialogue_memory.touch(now)
                    return self._decision(state, [{"type": "bounded_dialogue_turn", "intent": intent}], "support_dialogue_turn")
                return self._decision(state, [{"type": "support_response", "kind": intent}], "support_response")
            if state.mode == "personal_story_theme":
                if state.scene_menu_phase != "ready" or not getattr(self, 'personal_story_capable', False):
                    return self._decision(state, [], "story_theme_not_accepting")
                state.mode, state.state_version = "personal_story_generating", state.state_version + 1
                brief = {"age_band": state.age_band, "theme": intent, "forbidden_topics": ["diagnosis", "treatment", "real_schedule", "secrets"], "content_constraints": list(state.accessibility_policy.get("content_constraints", ()))}
                return self._decision(state, [{"type": "generate_personal_story", "brief": brief}], "personal_story_requested")
            if state.mode == "bear_dialogue":
                pending_information = state.dialogue_information_kind is not None
                state.dialogue_information_kind = None
                state.dialogue_memory.bounded_turns.append({"intent": intent})
                state.dialogue_memory.touch(now)
                state.state_version += 1
                # A model action is not the authority for real-world facts.
                # This runs only after the mandatory input safety/owner gates.
                from .canonical_services import conversational_information_intent
                information = conversational_information_intent(transient_text or "")
                if information:
                    return self._decision(state, [{'type':'audio_stop'}, {'type':'capture_close'},
                        self._information_effect(state, information)], 'typed_info_requested', clear_audio=True)
                return self._decision(state, ([{'type':'audio_stop'}, {'type':'capture_close'}] if pending_information else [])
                    + [{"type": "bounded_dialogue_turn", "intent": intent}], "dialogue_turn", clear_audio=pending_information)
            if state.mode == "schedule_weather":
                from .canonical_services import day_information_intent
                information = day_information_intent(transient_text or "")
                if information is None:
                    return self._decision(state, [], "info_request_ignored")
                if information in {"what_next", "where_now", 'card_doctors'} and state.profile_status != "identified":
                    return self._decision(state, [{"type": "privacy_blocked"}, {"type": "speak", "phrase_id": "PHRASE_101"}], "schedule_requires_profile")
                offered = {item[0] for item in getattr(self, 'day_menu_items', lambda sid: ())(state.session_id)}
                if information in {"date", "weather"} and information not in offered:
                    state.state_version += 1
                    return self._decision(state, [{"type": "audio_stop"}, {"type": "speak", "phrase_id": "PHRASE_100"}],
                                          "typed_information_unavailable", clear_audio=True)
                state.state_version += 1
                return self._decision(state, [self._information_effect(state, information)], "typed_info_requested")
            if state.mode == "story_paused" and intent in {"continue", "resume", "resume_story"}:
                if state.public_story_checkpoint_pending:
                    return self._decision(state, [], "public_story_checkpoint_pending")
                state.mode, state.substate, state.state_version = "story_resume_announcement", "story_resume_requested", state.state_version + 1
                return self._decision(state, [{"type": "lighting_wave_resume"}, {"type": "speak", "phrase_id": "PHRASE_064"}], "story_resume_requested", clear_audio=True)
            if state.mode == "story_paused" and intent in {"stop", "stop_story", "exit_story"}:
                return self._activity_back(state, 'story_returned_to_menu')
        if event.event_type == "HardwareReconnected":
            state.state_version += 1
            return self._decision(state, [{"type": "hardware_reconnected", "reconcile_required": True}], "hardware_reconnected")
        if event.event_type in {"ButtonReleased", "ButtonHeld", "ButtonLongHeld"}:
            return self._decision(state, [{"type": "button_edge_ack", "button_id": event.payload.get("button_id"), "edge": event.event_type}], "button_edge_ack")
        return self._decision(state, [], "event_ignored")
