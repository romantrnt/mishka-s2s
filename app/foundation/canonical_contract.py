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
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from .age_policy import age_details, policy_for, policy_for_band

RAM_TTL_SECONDS = 300
EXHIBITION_WELCOME_DELAY_SECONDS = 4.0
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
    "idle", "rfid_identified", "anonymous_visual", "visual_welcome",
    "awaiting_handset", "voice_welcome", "exhibition_introduction",
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
    "wellbeing_check", "safety_handoff", "bear_dialogue", "schedule_weather",
    "soft_end", "session_closed",
})
ALLOWED_EVENTS = frozenset({
    "RfidPresented", "RfidRemoved", "AnonymousStart", "HandsetLifted", "HandsetPlaced",
    "ButtonPressed", "ButtonReleased", "ButtonHeld", "ButtonLongHeld", "SpeechStarted",
    "SpeechEnded", "VoiceIntentReady", "AudioStarted", "AudioCompleted", "AudioFailed",
    "StoryPositionUpdated", "TimeoutFired", "WelcomeTimerFired", "HardwareDisconnected", "HardwareReconnected",
    "SessionCancel", "SessionReset", "AdultHandoffAcknowledged", "AdultResolved",
    "ExhibitionOnboardingComplete", "ExhibitionMenuWaveComplete", "StoryChoiceWaveComplete", "MenuTutorialStep",
})
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
        if self.event_type not in ALLOWED_EVENTS:
            raise ValueError("event type is not allowlisted")
        if not self.session_id or self.sequence < 0 or self.state_version < 0:
            raise ValueError("event identity is invalid")
        if self.event_type.startswith("Button"):
            button = self.payload.get("button_id")
            if isinstance(button, bool) or not isinstance(button, int) or button not in range(1, 8):
                raise ValueError("button_id must be in range 1..7")


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
    timeout_at: float | None = None
    history_tail: list[str] = field(default_factory=list)
    profile_ref: str | None = None
    profile_status: str = "anonymous"
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
    selected_menu_button: int | None = None
    selected_menu_action: str | None = None
    grammatical_gender: str = "neutral"
    language: str = "ru-RU"
    timezone: str = "Europe/Moscow"
    age_years: int | None = None
    profile_data_incomplete: bool = False
    current_date: str | None = None
    age_policy: dict[str, Any] = field(default_factory=dict)
    mood_result: str | None = None
    accessibility_policy: dict[str, Any] = field(default_factory=dict)
    schedule_ref: str | None = None
    handset: str = "down"
    expected_inputs: list[str] = field(default_factory=list)
    welcome_t0_monotonic: float | None = None
    welcome_deadline_monotonic: float | None = None
    welcome_status: str = "idle"
    safety_severity: str = "none"
    active_story_id: str | None = None
    game_target: int | None = None
    game_level: int = 0
    active_game_id: str | None = None
    game_parameters: dict[str, Any] = field(default_factory=dict)
    game_attempts: int = 0
    game_hint_stage: int = 0
    game_success_streak: int = 0
    dialogue_memory: DialogueMemory = field(default_factory=DialogueMemory)
    seen_correlations: set[str] = field(default_factory=set, repr=False)
    last_sequence_by_source: dict[str, int] = field(default_factory=dict, repr=False)

    def snapshot(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("seen_correlations", None)
        value.pop("last_sequence_by_source", None)
        # Profile display fields and schedule links stay inside the protected profile boundary.
        value.pop("display_name", None)
        value.pop("schedule_ref", None)
        return value


def canonical_state_nodes() -> dict[str, dict[str, Any]]:
    """Minimum scenario metadata required by the product contract."""
    return {
        name: {
            "state_version": 1,
            "allowed_events": sorted(ALLOWED_EVENTS), "expected_input": "contextual",
            "lighting_preset": "READY", "audio_policy": "application_owned",
            "timeout_policy": "explicit", "persistence_policy": "minimal",
            "success_transition": "explicit", "retry_transition": "explicit",
            "cancel_transition": "soft_end", "error_transition": "safe_stop",
        } for name in sorted(STATE_NAMES)
    }


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
        # refreshes cannot repeatedly decode all 106 files.
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
                lines = [line.strip() for line in source_path.read_text(encoding="utf-8").splitlines() if line.strip()]
                transcripts = [str(item.get("transcript")) for item in raw]
                if lines != transcripts:
                    errors.append({"code": "source_catalog_divergence", "source_catalog": source_ref})
            except (OSError, UnicodeError) as exc:
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
        return dict(item)

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
    bounds = {"1-3": (45, 70), "3-5": (60, 100), "5-7": (90, 150)}.get(str(brief.get("age_band")))
    if isinstance(duration, bool) or not isinstance(duration, int) or not bounds or not bounds[0] <= duration <= bounds[1]:
        return False, "duration"
    return True, None


def normalize_exhibition_name(value: str) -> str | None:
    """Extract a spoken name from a short answer.

    STT returns the whole utterance, not just the semantic slot.  In
    particular, ``меня зовут Вася`` must produce ``Вася`` rather than the
    first token (``меня``).  Keep only a bounded name-shaped prefix and never
    retain the transcript itself in session state.
    """
    text = " ".join(str(value or "").strip().split())
    if not text or len(text) > 160:
        return None
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
    """Extract an exact age 1..7 from a natural STT answer."""
    text = " ".join(str(value or "").casefold().strip().split())
    if not text or len(text) > 120:
        return None
    # Prefer an explicit one-digit age.  Do not accept a digit embedded in a
    # larger number or a year.
    match = re.search(r"(?<!\d)([1-7])(?!\d)", text)
    if match:
        return int(match.group(1))
    for word, age in _EXHIBITION_AGE_WORDS.items():
        if re.search(rf"(?<![а-яё]){re.escape(word)}(?![а-яё])", text):
            return age
    return None


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


class CanonicalRuntime:
    """In-memory state machine with stale-event, effect and RAM-TTL guarantees."""
    def __init__(self, *, now: Callable[[], float] = _utc_now, monotonic_now: Callable[[], float] = time.monotonic, stories: FixedStoryManifest | None = None, allow_development_placeholders: bool = False, exhibition_mode: bool = False) -> None:
        self.now, self.monotonic_now, self.stories, self.sessions = now, monotonic_now, stories, {}
        self.allow_development_placeholders = bool(allow_development_placeholders)
        self.exhibition_mode = bool(exhibition_mode)
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

    def _decision(self, state: CanonicalState, effects: list[dict[str, Any]], reason: str, *,
                  reset_session: bool = False, clear_audio: bool = False) -> dict[str, Any]:
        state.substate = reason
        state.timeout_at = state.dialogue_memory.expires_at
        audio_effect = next((item for item in effects if item.get("type") in {
            "speak", "play_fixed_story", "resume_fixed_story", "play_placeholder",
            "story_audio_resume", "speak_dynamic", "support_question", "support_response",
        }), None)
        if audio_effect is not None:
            content_id = str(audio_effect.get("phrase_id") or audio_effect.get("story_id") or
                              audio_effect.get("content_id") or "dynamic")
            state.active_audio_id = content_id
            state.playback_lease = f"{state.session_id}:{state.state_version}:{content_id}"
            state.audio_status = "queued"
            state.audio_failure_code = None
            audio_effect.setdefault("playback_lease", state.playback_lease)
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

    def handle(self, event: CanonicalEvent, profile: Mapping[str, Any] | None = None, *, transient_text: str | None = None) -> dict[str, Any]:
        event.validate()
        now = event.timestamp or self.now()
        monotonic_now = event.monotonic_timestamp if event.monotonic_timestamp is not None else self.monotonic_now()
        state = self.sessions.get(event.session_id)
        if state is None:
            # Let the first RFID pass through the explicit RfidPresented
            # branch below; seeding the new state with its profile makes that
            # first event indistinguishable from a repeated read.
            initial_profile = None if event.event_type == "RfidPresented" else profile
            state = self._new_state(event.session_id, initial_profile)
            self.sessions[event.session_id] = state
        if event.correlation_id and event.correlation_id in state.seen_correlations:
            return self._decision(state, [], "idempotent_duplicate")
        if event.state_version != state.state_version:
            return self._decision(state, [], "stale_state_version")
        prior_sequence = state.last_sequence_by_source.get(event.source, -1)
        if event.sequence <= prior_sequence:
            return self._decision(state, [], "stale_sequence")
        state.last_sequence_by_source[event.source] = event.sequence
        if event.correlation_id:
            state.seen_correlations.add(event.correlation_id)
        if event.event_type == "HandsetPlaced" and state.handset == "down":
            return self._decision(state, [], "handset_already_placed")
        if event.event_type == "HandsetLifted" and state.handset == "up":
            return self._decision(state, [], "handset_already_lifted")
        if event.event_type != "TimeoutFired" and state.dialogue_memory.expires_at and now >= state.dialogue_memory.expires_at:
            state.dialogue_memory.clear()
            if state.mode in {"dialogue_suspended_ram", "story_suspended_ram"}:
                state.mode = "dialogue_memory_expired"
        effects: list[dict[str, Any]] = []
        if event.event_type == "SessionCancel":
            state.dialogue_memory.clear(); state.mode = "soft_end"; state.state_version += 1
            return self._decision(state, [{"type": "audio_stop"}, {"type": "capture_close"}, {"type": "lighting_preset", "preset": "READY"}], "soft_end")
        if event.event_type == "SessionReset":
            state.dialogue_memory.clear(); state.mode = "session_closed"; state.state_version += 1
            return self._decision(state, [{"type": "audio_stop"}, {"type": "capture_close"}, {"type": "lighting_preset", "preset": "READY"}], "session_closed")
        # A closed/soft-ended session is inert until a new RFID or explicit
        # anonymous start creates a fresh scenario context. Stray handset,
        # audio and button events must not resurrect it.
        if state.mode in {"soft_end", "session_closed"} and event.event_type not in {"RfidPresented", "AnonymousStart", "SessionReset"}:
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
            return self._decision(state, [{
                "type": "speak", "phrase_id": "PHRASE_001",
                "scheduled_deadline_monotonic": deadline,
                "started_at_monotonic": monotonic_now,
            }], "welcome_started")
        if event.event_type == "TimeoutFired":
            if state.mode == "game_active":
                state.game_attempts += 1
                state.game_hint_stage = min(2, state.game_hint_stage + 1)
                if state.game_attempts >= 3 and state.game_level > 0:
                    state.game_level -= 1
                    state.game_target = ((state.game_level * 3) % 7) + 1
                    state.game_attempts = 0
                state.state_version += 1
                return self._decision(state, [{"type": "game_retry", "hint_stage": state.game_hint_stage, "target_button": state.game_target, "level": state.game_level}], "game_timeout_hint")
            if state.dialogue_memory.expires_at and now >= state.dialogue_memory.expires_at:
                state.dialogue_memory.clear()
                if state.mode in {"dialogue_suspended_ram", "story_suspended_ram"}:
                    state.mode = "dialogue_memory_expired"
                state.state_version += 1
                return self._decision(state, [{"type": "audio_stop"}, {"type": "clear_private_display"}], "ram_ttl_expired")
            return self._decision(state, [], "timeout_not_due")
        if event.event_type == "RfidRemoved":
            state.dialogue_memory.clear()
            state.profile_ref, state.profile_status = None, "anonymous"
            if state.mode == "safety_handoff":
                # Removing a tag cannot dismiss an active safeguarding
                # incident; the incident remains until AdultResolved.
                state.state_version += 1
                return self._decision(state, [{"type": "audio_stop"}, {"type": "capture_close"}, {"type": "lighting_preset", "preset": "SAFE_STOP"}], "rfid_removed_safety_preserved")
            state.mode, state.expected_inputs, state.state_version = "anonymous_visual", ["handset_up"], state.state_version + 1
            return self._decision(state, [{"type": "audio_stop"}, {"type": "capture_close"}, {"type": "clear_private_display"}], "rfid_removed")
        if event.event_type == "StoryPositionUpdated":
            state.dialogue_memory.story_checkpoint = {"story_id": state.active_story_id, "offset_ms": int(event.payload.get("offset_ms", 0))}
            state.state_version += 1
            return self._decision(state, [], "story_checkpoint_updated")
        if event.event_type in {"AudioCompleted", "AudioFailed"}:
            response_id = str(event.payload.get("response_id") or event.payload.get("content_id") or "")
            if state.active_audio_id and response_id != state.active_audio_id:
                return self._decision(state, [], "event_ignored")
            if event.payload.get("playback_lease") and event.payload.get("playback_lease") != state.playback_lease:
                return self._decision(state, [], "event_ignored")
        if event.event_type == "AudioCompleted" and self.exhibition_mode and state.mode == "voice_welcome":
            if str(event.payload.get("response_id") or "") != "PHRASE_001" or state.welcome_status != "started":
                return self._decision(state, [], "exhibition_welcome_completion_ignored")
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
            state.audio_failure_code = str(event.payload.get("error_code") or event.payload.get("code") or "audio_failed")[:80]
            state.audio_status = "failed"
            state.expected_inputs = ["handset_down", "reset"]
            state.state_version += 1
            return self._decision(state, [{"type": "content_blocked", "error_code": state.audio_failure_code},
                                          {"type": "lighting_preset", "preset": "ERROR_RECOVERABLE"}],
                                  "audio_failed_safe_stop", clear_audio=True)
        if event.event_type == "RfidPresented" and self.exhibition_mode:
            return self._decision(state, [], "exhibition_rfid_ignored")
        if event.event_type == "RfidPresented":
            incoming = _opaque_profile_ref(profile)
            if state.profile_ref and incoming == state.profile_ref and state.profile_status == "identified":
                # Re-reading the same tag is a valid physical-session event,
                # but it must not reset mode or extend suspended dialogue RAM.
                return self._decision(state, [], "rfid_repeated")
            if state.profile_ref and incoming != state.profile_ref:
                state.dialogue_memory.clear()
            state = self._new_state(event.session_id, profile)
            self.sessions[event.session_id] = state
            # An unknown tag is never treated as an identified child. It gets
            # the anonymous 3-5 flow and cannot unlock profile-backed tools.
            known_profile = state.profile_status == "identified"
            state.mode, state.expected_inputs, state.state_version = (
                ("visual_welcome" if known_profile else "anonymous_visual"),
                ["handset_up"],
                state.state_version + 1,
            )
            reason = "rfid_identified" if known_profile else "unknown_tag"
            return self._decision(state, [
                {"type": "lighting", "elements": [14], "state": 1},
                {"type": "visual", "view": "handset_invitation"},
                {"type": "lighting_preset", "preset": "ALL_ON"},
            ], reason)
        if event.event_type == "AnonymousStart":
            state.profile_ref, state.profile_status, state.age_band, state.age_band_source = None, "anonymous", "3-5", "anonymous_default"
            state.mode, state.state_version = "anonymous_visual", state.state_version + 1
            return self._decision(state, [{"type": "lighting", "elements": [14], "state": 1}], "anonymous_started")
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
                    {"type": "standby_breathing_started"},
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
            if state.mode != "safety_handoff":
                state.dialogue_memory.suspend(now)
            if state.mode == "game_active":
                effects = [{"type": "audio_stop"}, {"type": "capture_close"}, {"type": "game_continue_visual"}]
            elif state.mode == "safety_handoff":
                # The safeguarding marker is not dialogue RAM: handset-down
                # closes capture/audio but keeps the incident visible and
                # awaiting adult resolution.
                effects = [{"type": "audio_stop"}, {"type": "capture_close"}, {"type": "lighting_preset", "preset": "SAFE_STOP"}]
            elif state.mode == "fixed_story_playing":
                state.dialogue_memory.story_checkpoint = {"story_id": state.active_story_id, "offset_ms": int(event.payload.get("offset_ms", 0))}
                state.mode = "story_suspended_ram"; effects = [{"type": "audio_stop"}, {"type": "capture_close"}]
            else:
                state.mode = "dialogue_suspended_ram"; effects = [{"type": "audio_stop"}, {"type": "capture_close"}, {"type": "clear_private_display"}]
            state.state_version += 1
            return self._decision(state, effects, "handset_down")
        if event.event_type == "HandsetLifted":
            state.handset = "up"; state.state_version += 1
            if state.mode == "idle":
                state.mode = "voice_welcome"
                state.expected_inputs = ["handset_down"]
                state.welcome_t0_monotonic = monotonic_now
                state.welcome_deadline_monotonic = monotonic_now + EXHIBITION_WELCOME_DELAY_SECONDS
                state.welcome_status = "pending"
                return self._decision(state, [
                    {"type": "lighting_wave_intent", "pattern": "handset_lift_wave", "source": "canonical"},
                    {"type": "play_asset", "asset_id": "assets/sounds/ringtone.wav", "content_id": "RINGTONE"},
                    {"type": "welcome_timer_scheduled", "phrase_id": "PHRASE_001",
                     "delay_seconds": EXHIBITION_WELCOME_DELAY_SECONDS,
                     "t0_monotonic": state.welcome_t0_monotonic,
                     "deadline_monotonic": state.welcome_deadline_monotonic},
                ], "exhibition_handset_lifted")
            if state.mode == "safety_handoff":
                state.expected_inputs = ["adult_resolution"]
                return self._decision(state, [{"type": "lighting_preset", "preset": "SAFE_STOP"}], "safety_handoff_active")
            if state.mode == "game_active":
                state.expected_inputs = ["button", "speech", "handset_down"]
                return self._decision(state, [{"type": "capture_open"}, {"type": "lighting_preset", "preset": "LISTENING"}], "game_voice_join")
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
                    return self._decision(state, [story_wave, {"type": "play_fixed_story", "story_id": story_id, "asset": verified["audio_file"]}], "fixed_story_started_after_handset")
                if placeholder is not None:
                    return self._decision(state, [story_wave, {"type": "play_placeholder", "content_kind": "fixed_story", "story_id": story_id, "asset": placeholder["audio_file"], "development_only": True}], "fixed_story_placeholder_after_handset")
            if state.mode in {"game_menu", "fixed_story_menu", "support_intake", "personal_story_theme", "wellbeing_check", "bear_dialogue", "schedule_weather"}:
                state.expected_inputs = ["button", "speech", "handset_down"]
                return self._decision(state, [{"type": "capture_open"}, {"type": "lighting_preset", "preset": "LISTENING"}], "voice_open_activity")
            if state.profile_status == "identified":
                state.mode = "automatic_mood_check"
                state.expected_inputs = ["button", "speech", "handset_down"]
                greeting = f"Привет, {state.display_name}. Я Мишка" if state.display_name else "Привет. Я Мишка"
                return self._decision(state, [{"type": "capture_open"}, {"type": "speak_dynamic", "text": greeting, "personalized": bool(state.display_name)}, {"type": "speak", "phrase_id": "PHRASE_014"}, {"type": "lighting_preset", "preset": "MOOD_CHOICES"}], "automatic_mood_check")
            if state.pending_mode:
                pending = state.pending_mode
                state.pending_mode = None
                state.mode = pending
                state.expected_inputs = ["button", "speech", "handset_down"]
                return self._decision(state, [{"type": "capture_open"}, {"type": "lighting_preset", "preset": "LISTENING"}], "voice_open_pending_activity")
            state.mode = "voice_welcome"
            state.expected_inputs = ["button", "speech", "handset_down"]
            return self._decision(state, [{"type": "capture_open"}, {"type": "lighting_preset", "preset": "LISTENING"}], "voice_open")
        if event.event_type == "ButtonReleased" and self.exhibition_mode and state.mode == "menu_ready":
            button = int(event.payload["button_id"])
            if button not in state.menu_pressed_buttons:
                return self._decision(state, [], "exhibition_menu_button_release_ignored")
            state.menu_pressed_buttons.remove(button)
            state.state_version += 1
            return self._decision(state, [], "exhibition_menu_button_released")
        if event.event_type == "ButtonPressed":
            button = int(event.payload["button_id"])
            if self.exhibition_mode and state.mode in {"idle", "voice_welcome"}:
                return self._decision(state, [], "exhibition_age_not_ready")
            if state.mode == "safety_handoff":
                return self._decision(state, [{"type": "lighting_preset", "preset": "SAFE_STOP"}], "safety_handoff_active")
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
                state.menu_pressed_buttons = []
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
                effects = [selection]
                if button == 7:
                    # The current exhibition build has no live calendar/weather
                    # provider in this anonymous flow. Keep the button semantic
                    # and give the child a bounded, audible fallback instead of
                    # emitting a selection with no downstream effect.
                    effects.append({
                        "type": "speak", "phrase_id": "PHRASE_099",
                        "fallback_for": "day_and_plans",
                        "resolution": "exhibition_day_plans_fallback",
                    })
                    selection["downstream_owner"] = "exhibition_day_plans_fallback"
                return self._decision(state, effects, "exhibition_menu_selected")
            if state.mode == "session_closed":
                return self._decision(state, [], "session_closed")
            if button == 5 and state.mode in {
                "idle", "visual_welcome", "anonymous_visual", "root_menu",
                "dialogue_memory_expired", "dialogue_suspended_ram", "voice_welcome",
                "awaiting_handset", "wellbeing_check",
                "game_active", "fixed_story_playing", "story_paused", "fixed_story_menu",
                "game_menu", "support_intake", "personal_story_theme", "bear_dialogue",
                "schedule_weather",
            } and not (self.exhibition_mode and state.mode == "exhibition_age_selection"):
                state.mode, state.expected_inputs, state.state_version = "wellbeing_check", ["button_1", "button_7", "speech"], state.state_version + 1
                return self._decision(state, [{"type": "audio_stop"}, {"type": "lighting_preset", "preset": "HELP"}, {"type": "support_question", "question": "pain_or_discomfort"}], "help_priority")
            if self.exhibition_mode and state.mode == "exhibition_age_selection":
                if button not in range(1, 8):
                    return self._decision(state, [], "exhibition_age_button_invalid")
                return self._select_exhibition_age(state, button, source="exhibition_button", now=now)
            if state.mode == "automatic_mood_check":
                mood = "low" if button in {1, 2, 3} else "calm" if button in {4, 5} else "good"
                state.mood_result = mood
                state.mode, state.state_version = "root_menu", state.state_version + 1
                return self._decision(state, [{"type": "mood_report", "value": mood, "input": f"button_{button}"}, {"type": "speak", "phrase_id": "PHRASE_017"}, {"type": "lighting_preset", "preset": "ROOT_MENU"}], "mood_report")
            if state.mode in {"fixed_story_playing", "story_speech_analysis"}:
                if button == 6:
                    state.mode, state.substate = "story_pause_announcement", "story_pause_requested"
                    state.state_version += 1
                    state.dialogue_memory.story_checkpoint = {"story_id": state.active_story_id, "offset_ms": int(event.payload.get("offset_ms", 0))}
                    state.expected_inputs = ["audio_completed", "button_7", "handset_down"]
                    return self._decision(state, [
                        {"type": "story_audio_pause", "story_id": state.active_story_id},
                        {"type": "lighting_wave_pause"},
                        {"type": "lighting_story_pause_flash"},
                        {"type": "speak", "phrase_id": "PHRASE_062"},
                    ], "story_pause_requested", clear_audio=True)
                if button == 7:
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
                state.mode, state.state_version = "root_menu", state.state_version + 1
                return self._decision(state, [{"type": "lighting_preset", "preset": "ROOT_MENU"}], "story_menu_back")
            if state.mode == "game_menu" and button == 7:
                state.mode, state.state_version = "root_menu", state.state_version + 1
                return self._decision(state, [{"type": "lighting_preset", "preset": "ROOT_MENU"}], "game_menu_back")
            if state.mode in {"support_intake", "bear_dialogue"} and button == 7:
                state.mode, state.state_version = "root_menu", state.state_version + 1
                return self._decision(state, [{"type": "audio_stop"}, {"type": "capture_close"}, {"type": "lighting_preset", "preset": "ROOT_MENU"}], "activity_back")
            if state.mode == "wellbeing_check" and button == 5:
                return self._decision(state, [{"type": "support_question", "question": "pain_or_discomfort"}], "wellbeing_repeat")
            if state.mode == "wellbeing_check" and button == 7:
                state.mode, state.safety_severity, state.state_version = "safety_handoff", "concern", state.state_version + 1
                return self._decision(state, [{"type": "adult_handoff_request", "severity": "concern"}, {"type": "lighting_preset", "preset": "SAFE_STOP"}], "adult_handoff_requested")
            if state.mode == "wellbeing_check" and button == 1:
                state.mode, state.state_version = "support_intake", state.state_version + 1
                return self._decision(state, [{"type": "support_question", "question": "fear"}], "support_continue")
            if state.mode in {"idle", "visual_welcome", "anonymous_visual", "root_menu", "dialogue_memory_expired", "dialogue_suspended_ram", "voice_welcome", "automatic_mood_check", "awaiting_handset"}:
                mode = BUTTON_MODES[button]
                if button in {3, 6} and state.handset != "up":
                    state.pending_mode = mode
                    state.mode, state.state_version = "awaiting_handset", state.state_version + 1
                    return self._decision(state, [{"type": "lighting", "elements": [14], "state": 1}, {"type": "visual", "view": "lift_handset"}], "handset_required")
                state.mode, state.state_version = mode, state.state_version + 1
                state.expected_inputs = (["speech", "button_7", "handset_down"]
                                         if mode == "bear_dialogue" and state.handset == "up"
                                         else ["button", "speech"] if state.handset == "up" else ["button"])
                preset = {
                    "game_menu": "GAME_MENU", "fixed_story_menu": "STORY_MENU",
                    "support_intake": "SUPPORT", "personal_story_theme": "PERSONAL_STORY",
                    "wellbeing_check": "HELP", "bear_dialogue": "BEAR_DIALOGUE",
                    "schedule_weather": "SCHEDULE_WEATHER",
                }.get(mode, "ROOT_MENU")
                effects = [{"type": "lighting_preset", "preset": preset}]
                if mode == "bear_dialogue" and state.handset == "up":
                    state.substate = "dialogue_started"
                    state.dialogue_memory.touch(now)
                    effects.extend([
                        {"type": "capture_open"},
                        {"type": "speak", "phrase_id": "PHRASE_094"},
                    ])
                    return self._decision(state, effects, "bear_dialogue_started", clear_audio=True)
                return self._decision(state, effects, "button_mode")
            if state.mode == "personal_story_theme":
                themes = {1: "dinosaurs", 2: "космос", 3: "лес", 4: "дружелюбные машины", 6: "море"}
                if button == 7:
                    state.mode, state.state_version = "root_menu", state.state_version + 1
                    return self._decision(state, [{"type": "lighting_preset", "preset": "ROOT_MENU"}], "personal_story_cancelled")
                if button in themes:
                    state.mode, state.state_version = "personal_story_generating", state.state_version + 1
                    brief = {"age_band": state.age_band, "theme": themes[button], "forbidden_topics": ["diagnosis", "treatment", "real_schedule", "secrets"], "content_constraints": list(state.accessibility_policy.get("content_constraints", ())), "display_name": state.display_name}
                    return self._decision(state, [{"type": "generate_personal_story", "brief": brief}], "personal_story_requested_by_button")
            if state.mode == "story_paused" and button == 6:
                state.mode, state.substate, state.state_version = "story_resume_announcement", "story_resume_requested", state.state_version + 1
                state.expected_inputs = ["audio_completed", "button_7", "handset_down"]
                return self._decision(state, [{"type": "lighting_wave_resume"}, {"type": "speak", "phrase_id": "PHRASE_064"}], "story_resume_requested", clear_audio=True)
            if state.mode == "game_menu":
                selected = GAME_CATALOG[(button - 1) % len(GAME_CATALOG)]
                selected_level = max(int(selected["level"]), min(9, int(state.game_level)))
                state.mode, state.game_level, state.game_target, state.active_game_id, state.game_parameters, state.game_attempts, state.game_hint_stage, state.game_success_streak, state.state_version = "game_active", selected_level, 1, selected["game_id"], {**dict(selected), "profile_level": selected_level}, 0, 0, 0, state.state_version + 1
                return self._decision(state, [{"type": "lighting_preset", "preset": "TARGET", "target_button": 1}, {"type": "game_level", "level": state.game_level, "max_level": 9, "game_id": selected["game_id"], "catalog": list(GAME_CATALOG)}], "game_started")
            if state.mode == "game_active" and state.substate == "game_exit_confirmation":
                if button == 7:
                    state.mode, state.substate, state.state_version = "root_menu", "game_exit_confirmed", state.state_version + 1
                    return self._decision(state, [{"type": "audio_stop"}, {"type": "lighting_preset", "preset": "ROOT_MENU"}], "game_exit_confirmed")
                if button == 6:
                    state.substate, state.state_version = "game_active", state.state_version + 1
                    return self._decision(state, [{"type": "lighting_preset", "preset": "TARGET", "target_button": state.game_target}], "game_exit_cancelled")
            if state.mode == "game_active" and button == 7:
                state.substate, state.state_version = "game_exit_confirmation", state.state_version + 1
                return self._decision(state, [{"type": "game_exit_confirmation", "requires_explicit_choice": True}], "game_exit_confirmation")
            if state.mode == "game_active" and button == state.game_target:
                if state.game_level >= 9:
                    state.mode, state.game_target, state.state_version = "root_menu", None, state.state_version + 1
                    return self._decision(state, [{"type": "lighting_preset", "preset": "SUCCESS"}, {"type": "game_completed", "level": 9}], "game_completed")
                state.game_success_streak += 1
                if state.game_success_streak >= 2:
                    state.game_level += 1
                    state.game_success_streak = 0
                state.game_target, state.state_version = ((state.game_level * 3) % 7) + 1, state.state_version + 1
                state.dialogue_memory.touch(now)
                state.game_attempts = 0
                state.game_hint_stage = 0
                return self._decision(state, [{"type": "lighting_preset", "preset": "SUCCESS"}, {"type": "lighting_preset", "preset": "TARGET", "target_button": state.game_target}, {"type": "game_level", "level": state.game_level, "max_level": 9, "mastery_window": {"success": 7, "attempts": 10, "without_hint": 5}}], "game_progress")
            if state.mode == "game_active":
                state.game_attempts += 1
                state.game_hint_stage = min(2, state.game_hint_stage + 1)
                if state.game_attempts >= 2 and state.game_level > 0:
                    state.game_level -= 1
                    state.game_target = ((state.game_level * 3) % 7) + 1
                    state.game_attempts = 0
                return self._decision(state, [{"type": "game_retry", "hint_stage": state.game_hint_stage, "target_button": state.game_target, "level": state.game_level}], "game_retry")
            if state.mode == "fixed_story_menu":
                story_id = self._story_id_for_button(button)
                if story_id is None:
                    return self._decision(state, [], "story_menu_ignored")
                verified = self.stories.get_verified(story_id) if self.stories else None
                placeholder = self.stories.get_development_placeholder() if self.stories and self.allow_development_placeholders else None
                if verified is None and placeholder is None:
                    return self._decision(state, [{"type": "content_blocked", "story_id": story_id}, {"type": "lighting_preset", "preset": "ERROR_RECOVERABLE"}], "fixed_story_blocked")
                state.active_story_id, state.state_version = story_id, state.state_version + 1
                if state.handset != "up":
                    state.pending_mode = "fixed_story_playing"
                    state.mode = "awaiting_handset"
                    return self._decision(state, [{"type": "lighting", "elements": [14], "state": 1}, {"type": "visual", "view": "lift_handset"}], "story_handset_required")
                state.mode, state.substate = "fixed_story_playing", "story_started"
                state.expected_inputs = ["button_6", "button_7", "handset_down"]
                story_wave = {"type": "lighting_wave_intent", "pattern": "story_playback_wave", "groups": "story_playback", "source": "canonical", "loop": True, "stage_ms": 620}
                if verified is not None:
                    return self._decision(state, [story_wave, {"type": "play_fixed_story", "story_id": story_id, "asset": verified["audio_file"]}], "fixed_story_started")
                return self._decision(state, [story_wave, {"type": "play_placeholder", "content_kind": "fixed_story", "story_id": story_id, "asset": placeholder["audio_file"], "development_only": True}], "fixed_story_placeholder")
            if state.mode == "schedule_weather":
                action = {1: "date", 2: "weather", 3: "what_next", 4: "where_now", 7: "back"}.get(button)
                if action == "back":
                    state.mode, state.state_version = "root_menu", state.state_version + 1
                    return self._decision(state, [{"type": "lighting_preset", "preset": "ROOT_MENU"}], "info_back")
                if action in {"what_next", "where_now"} and state.profile_status != "identified":
                    return self._decision(state, [{"type": "privacy_blocked"}, {"type": "offer_adult"}], "schedule_requires_profile")
                if action:
                    state.state_version += 1
                    return self._decision(state, [{"type": "typed_info_request", "kind": action}], "typed_info_requested")
        if event.event_type == "VoiceIntentReady":
            severity = str(event.payload.get("safety_severity", "none"))
            if severity not in {"none", "concern", "urgent"}:
                return self._decision(state, [], "invalid_safety_severity")
            if severity == "urgent":
                state.mode, state.safety_severity, state.state_version = "safety_handoff", "urgent", state.state_version + 1
                return self._decision(state, [{"type": "audio_stop"}, {"type": "adult_handoff_request", "severity": "urgent"}, {"type": "lighting_preset", "preset": "SAFE_STOP"}], "urgent_safety")
            intent = str(event.payload.get("intent", "free_speech"))
            if intent == "request_adult" or (state.mode == "wellbeing_check" and intent == "yes"):
                state.mode, state.safety_severity, state.state_version = "safety_handoff", "concern", state.state_version + 1
                return self._decision(state, [{"type": "adult_handoff_request", "severity": "concern"}, {"type": "lighting_preset", "preset": "SAFE_STOP"}], "adult_requested")
            if state.handset != "up" and state.mode not in {"safety_handoff"}:
                return self._decision(state, [{"type": "visual", "view": "lift_handset"}], "voice_requires_handset")
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
                return self._decision(state, [{"type": "story_audio_pause", "story_id": state.active_story_id}, {"type": "lighting_wave_pause"}, {"type": "lighting_story_pause_flash"}, {"type": "speak", "phrase_id": "PHRASE_062"}], "story_pause_requested", clear_audio=True)
            if state.mode in {"fixed_story_playing", "story_speech_analysis"} and intent in {"stop", "stop_story", "exit_story"}:
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
            if state.mode == "personal_story_generating" and intent == "generated":
                state.mode, state.state_version = "personal_story_validating", state.state_version + 1
                return self._decision(state, [{"type": "validate_personal_story"}], "personal_story_generated")
            if state.mode == "personal_story_generating" and intent == "validation_failed":
                state.mode, state.state_version = "personal_story_theme", state.state_version + 1
                return self._decision(state, [{"type": "speak", "phrase_id": "PHRASE_080"}, {"type": "speak", "phrase_id": "PHRASE_081"}], "personal_story_fallback")
            if state.mode == "personal_story_validating" and intent == "validated":
                state.mode, state.state_version = "personal_story_playing", state.state_version + 1
                return self._decision(state, [{"type": "play_personal_story", "development_only": True}], "personal_story_validated")
            if self.exhibition_mode and state.mode == "exhibition_name_capture":
                name = normalize_exhibition_name(transient_text or "")
                if name is None:
                    state.state_version += 1
                    return self._decision(state, [
                        {"type": "speak", "phrase_id": "PHRASE_074"},
                        {"type": "capture_open"},
                        {"type": "exhibition_name_rejected"},
                    ], "exhibition_name_unrecognized")
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
            if severity == "concern":
                state.mode, state.safety_severity, state.state_version = "support_intake", "concern", state.state_version + 1
                return self._decision(state, [{"type": "support_response", "kind": "concern"}, {"type": "adult_handoff_offer"}], "concern_support")
            if state.mode in {"support_intake", "wellbeing_check"}:
                state.state_version += 1
                return self._decision(state, [{"type": "support_response", "kind": intent}], "support_response")
            if state.mode == "personal_story_theme":
                state.mode, state.state_version = "personal_story_generating", state.state_version + 1
                brief = {"age_band": state.age_band, "theme": intent, "forbidden_topics": ["diagnosis", "treatment", "real_schedule", "secrets"], "content_constraints": list(state.accessibility_policy.get("content_constraints", ())), "display_name": state.display_name}
                return self._decision(state, [{"type": "generate_personal_story", "brief": brief}], "personal_story_requested")
            if state.mode == "bear_dialogue":
                state.dialogue_memory.bounded_turns.append({"intent": intent})
                state.dialogue_memory.touch(now)
                state.state_version += 1
                return self._decision(state, [{"type": "bounded_dialogue_turn", "intent": intent}], "dialogue_turn")
            if state.mode == "schedule_weather" and intent == "where_now":
                if state.profile_status != "identified":
                    return self._decision(state, [{"type": "privacy_blocked"}], "schedule_requires_profile")
                state.state_version += 1
                return self._decision(state, [{"type": "typed_info_request", "kind": "where_now"}], "typed_info_requested")
            if state.mode == "story_paused" and intent in {"continue", "resume", "resume_story"}:
                state.mode, state.substate, state.state_version = "story_resume_announcement", "story_resume_requested", state.state_version + 1
                return self._decision(state, [{"type": "lighting_wave_resume"}, {"type": "speak", "phrase_id": "PHRASE_064"}], "story_resume_requested", clear_audio=True)
            if state.mode == "story_paused" and intent in {"stop", "free_speech"}:
                state.mode, state.state_version = "root_menu", state.state_version + 1
                return self._decision(state, [{"type": "lighting_preset", "preset": "ROOT_MENU"}], "story_returned_to_menu")
        if event.event_type == "AdultHandoffAcknowledged":
            if state.mode != "safety_handoff":
                return self._decision(state, [], "handoff_ack_ignored")
            state.state_version += 1
            return self._decision(state, [{"type": "speak", "phrase_id": "PHRASE_089"}, {"type": "lighting_preset", "preset": "SAFE_STOP"}], "adult_handoff_acknowledged")
        if event.event_type == "AdultResolved":
            if state.mode != "safety_handoff":
                return self._decision(state, [], "adult_resolved_ignored")
            state.mode, state.safety_severity, state.state_version = "root_menu", "none", state.state_version + 1
            return self._decision(state, [{"type": "lighting_preset", "preset": "ROOT_MENU"}, {"type": "adult_incident_closed"}], "adult_resolved")
        if event.event_type == "HardwareDisconnected":
            state.mode, state.state_version = "safety_handoff", state.state_version + 1
            return self._decision(state, [{"type": "audio_stop"}, {"type": "lighting_preset", "preset": "SAFE_STOP"}], "hardware_disconnected")
        if event.event_type == "HardwareReconnected":
            state.state_version += 1
            return self._decision(state, [{"type": "hardware_reconnected", "reconcile_required": True}], "hardware_reconnected")
        if event.event_type in {"ButtonReleased", "ButtonHeld", "ButtonLongHeld"}:
            return self._decision(state, [{"type": "button_edge_ack", "button_id": event.payload.get("button_id"), "edge": event.event_type}], "button_edge_ack")
        return self._decision(state, [], "event_ignored")
