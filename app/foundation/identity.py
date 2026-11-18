"""Bounded local profile validation and lifetime, not patient lookup or authorization.

Max supplies a validated card. These types constrain only what the scenario
engine may keep/use, while operator authentication remains separate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
import math
import re
import unicodedata
from typing import Any, Literal, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


IdentityStatus = Literal["authorized", "unknown", "denied", "unavailable"]
IDENTITY_CAPABILITIES = frozenset({"personalize", "schedule"})
_IDENTITY_STATUSES = frozenset({"authorized", "unknown", "denied", "unavailable"})
_FLAG_FIELDS = ("motor_flags", "sensory_flags", "communication_flags")
_TRIGGER_TOPICS = frozenset({
    "parent_absence", "limb_absence_or_difference", "separation", "loss",
    "medical_procedure_fear", "body_vulnerability", "bullying", "animal_fear",
    "darkness_fear", "loud_sound_fear",
})
_TRIGGER_HANDLING = frozenset({"avoid_parent_assumptions", "avoid_body_assumptions", "adult_neutral"})


class IdentityValidationError(ValueError):
    """A safe field-level error: values, profiles and UIDs are never included."""


def _bounded_text(value: Any, field_name: str, maximum: int) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(char) < 32 or 127 <= ord(char) < 160 or 0xD800 <= ord(char) <= 0xDFFF for char in value)):
        raise IdentityValidationError(f"{field_name} must be a nonempty bounded string")
    return value


def _card_doctor_facts(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Closed private card facts; the source marker never grants authorization."""
    fields = {"card_doctor_names", "card_visit_date", "card_doctors_source"}
    if not fields.intersection(profile):
        return {}
    names, visit_date = profile.get("card_doctor_names"), profile.get("card_visit_date")
    if (not fields.issubset(profile) or profile["card_doctors_source"] != "data14.v1"
            or not isinstance(names, list) or len(names) > 32
            or not isinstance(visit_date, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", visit_date)):
        raise IdentityValidationError("invalid DATA-14 doctor facts")
    try:
        date.fromisoformat(visit_date)
    except ValueError:
        raise IdentityValidationError("invalid DATA-14 visit date") from None
    copied = []
    for name in names:
        name = _bounded_text(name, "card_doctor_names", 160)
        if (any(char in "<>&[]{}*_`#\\" for char in name)
                or any(unicodedata.category(char) in {"Cf", "Zl", "Zp"} for char in name)):
            raise IdentityValidationError("invalid DATA-14 doctor name")
        copied.append(name)
    return {"card_doctor_names": copied, "card_visit_date": visit_date, "card_doctors_source": "data14.v1"}




def normalize_profile(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Copy only the bounded fields understood by the current scenario engine.

    Legacy child_id is an input alias only. Unknown DB fields (including UIDs,
    arbitrary schedule payloads and free-form notes) never cross this boundary.
    Invalid values in an allowed field invalidate the entire profile.
    """
    if not isinstance(profile, Mapping):
        raise IdentityValidationError("identity profile must be a mapping")
    reference = profile.get("profile_ref")
    legacy_reference = profile.get("child_id")
    if reference is not None:
        reference = _bounded_text(reference, "profile_ref", 128)
    if legacy_reference is not None:
        legacy_reference = _bounded_text(legacy_reference, "child_id", 128)
    if reference is not None and legacy_reference is not None and reference != legacy_reference:
        raise IdentityValidationError("identity profile references conflict")
    result: dict[str, Any] = {"profile_ref": reference or legacy_reference}
    if result["profile_ref"] is None:
        raise IdentityValidationError("identity profile requires profile_ref")
    result.update(_card_doctor_facts(profile))

    for name, maximum in (("display_name", 80), ("schedule_ref", 128)):
        if name in profile:
            result[name] = _bounded_text(profile[name], name, maximum)
    if "language" in profile:
        language = _bounded_text(profile["language"], "language", 35)
        if not re.fullmatch(r"[a-zA-Z]{2,3}(?:-[a-zA-Z0-9]{2,8}){0,3}", language):
            raise IdentityValidationError("language must be a bounded language tag")
        result["language"] = language
    zone_name = profile.get("timezone", "Europe/Moscow")
    zone_name = _bounded_text(zone_name, "timezone", 64)
    try:
        zone = ZoneInfo(zone_name)
    except (ValueError, ZoneInfoNotFoundError):
        raise IdentityValidationError("timezone must be a known timezone") from None
    if "timezone" in profile:
        result["timezone"] = zone_name
    if "date_of_birth" in profile:
        raw = profile["date_of_birth"]
        if not isinstance(raw, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            raise IdentityValidationError("date_of_birth must be an ISO date")
        try:
            born = date.fromisoformat(raw)
        except ValueError:
            raise IdentityValidationError("date_of_birth must be an ISO date") from None
        if born > datetime.now(zone).date():
            raise IdentityValidationError("date_of_birth must not be in the future")
        result["date_of_birth"] = born.isoformat()
    if "age_years" in profile:
        age = profile["age_years"]
        if (type(age) is not int or not 0 <= age <= 130
                or profile.get("age_source") != "data14_age_years"
                or profile.get("birth_data_state") not in {"complete", "needs_clarification"}):
            raise IdentityValidationError("invalid DATA-14 age provenance")
        result.update(age_years=age, age_source="data14_age_years", birth_data_state=profile["birth_data_state"])
    if "grammatical_gender" in profile:
        if profile["grammatical_gender"] not in ("masculine", "feminine", "neutral"):
            raise IdentityValidationError("grammatical_gender is unsupported")
        result["grammatical_gender"] = profile["grammatical_gender"]
    for name in _FLAG_FIELDS:
        if name not in profile:
            continue
        flags = profile[name]
        if not isinstance(flags, (list, tuple)) or len(flags) > 8:
            raise IdentityValidationError(f"{name} must be a bounded list")
        result[name] = [_bounded_text(flag, name, 40) for flag in flags]
    if "trigger_topics" in profile:
        topics = profile["trigger_topics"]
        if not isinstance(topics, (list, tuple)) or len(topics) > 8:
            raise IdentityValidationError("trigger_topics must be a bounded list")
        result["trigger_topics"] = []
        for topic in topics:
            if (not isinstance(topic, Mapping) or not isinstance(topic.get("topic_id"), str)
                    or topic["topic_id"] not in _TRIGGER_TOPICS
                    or topic.get("status") not in ("active", "inactive")):
                raise IdentityValidationError("trigger_topics contains an invalid topic")
            handling = topic.get("handling", "adult_neutral")
            if not isinstance(handling, str) or handling not in _TRIGGER_HANDLING:
                raise IdentityValidationError("trigger_topics contains unsupported handling")
            result["trigger_topics"].append({"topic_id": topic["topic_id"], "status": topic["status"], "handling": handling})
    return result


@dataclass(frozen=True)
class IdentityDecision:
    status: IdentityStatus
    profile: dict[str, Any] | None = field(default=None, repr=False)
    capabilities: frozenset[str] = frozenset()
    expires_monotonic: float = 0.0


def validate_identity_decision(decision: Any, now: float) -> IdentityDecision:
    """Validate a bounded local profile lease; this does not query or authorize a patient."""
    unavailable = IdentityDecision("unavailable")
    if (not isinstance(decision, IdentityDecision) or not isinstance(decision.status, str)
            or decision.status not in _IDENTITY_STATUSES):
        return unavailable
    if decision.status != "authorized":
        # Negative results cannot carry residual child context or a grant.
        return IdentityDecision(decision.status)
    if (isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now)
            or isinstance(decision.expires_monotonic, bool)
            or not isinstance(decision.expires_monotonic, (int, float))
            or not math.isfinite(decision.expires_monotonic) or decision.expires_monotonic <= now
            or not isinstance(decision.capabilities, frozenset)
            or not decision.capabilities <= IDENTITY_CAPABILITIES):
        return unavailable
    try:
        profile = normalize_profile(decision.profile)
    except (IdentityValidationError, TypeError, ValueError):
        return unavailable
    return IdentityDecision("authorized", profile, decision.capabilities, float(decision.expires_monotonic))
