"""Private identity boundary. No production reader or authorization service is implied.

UIDs are transient lookup inputs. An operator token, a legacy profile row, or a
matching UID alone never creates a child-data grant.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
import math
import re
import time
from typing import Any, Callable, Literal, Mapping, Protocol
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


def validate_uid(uid: Any) -> str:
    """Validate an opaque UID without changing case, leading zeroes or spacing."""
    return _bounded_text(uid, "RFID tag_id", 128)


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
    """Fail closed for every injected resolver, including malformed test doubles."""
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


class IdentityResolver(Protocol):
    async def resolve(self, uid: str, installation_id: str, request_id: str) -> IdentityDecision: ...
    def readiness(self) -> dict[str, Any]: ...


class DisabledIdentityResolver:
    async def resolve(self, uid: str, installation_id: str, request_id: str) -> IdentityDecision:
        return IdentityDecision("unavailable")

    def readiness(self) -> dict[str, Any]:
        return {"provider": "disabled", "ready": False, "synthetic": False,
                "production_ready": False, "reason": "disabled"}


class SyntheticIdentityResolver:
    """Explicit fixture decisions only; never automatically authorize a UID.

    Settings restrict this provider to mock + fake/simulator deployments. No
    fixtures are loaded from environment, disk, legacy SQLite or real services.
    """
    def __init__(self, fixtures: Mapping[str, IdentityDecision], *,
                 clock: Callable[[], float] = time.monotonic, lease_s: float = 60.0) -> None:
        if (isinstance(lease_s, bool) or not isinstance(lease_s, (int, float))
                or not math.isfinite(lease_s) or not 0 < lease_s <= 3600):
            raise IdentityValidationError("synthetic lease_s must be positive and bounded")
        self._clock = clock
        self._lease_s = float(lease_s)
        # Snapshot validated profiles so later caller mutations cannot change
        # the fixture that was explicitly supplied at construction.
        now = self._clock()
        self._fixtures = {validate_uid(uid): validate_identity_decision(decision, now)
                          for uid, decision in fixtures.items()}

    async def resolve(self, uid: str, installation_id: str, request_id: str) -> IdentityDecision:
        try:
            uid = validate_uid(uid)
        except IdentityValidationError:
            return IdentityDecision("unavailable")
        now = self._clock()
        decision = validate_identity_decision(self._fixtures.get(uid, IdentityDecision("unknown")), now)
        if decision.status != "authorized":
            return decision
        # A child reference must be independent of the presented card token.
        if decision.profile["profile_ref"] == uid or decision.profile.get("schedule_ref") == uid:
            return IdentityDecision("unavailable")
        return IdentityDecision("authorized", decision.profile, decision.capabilities,
                                min(decision.expires_monotonic, now + self._lease_s))

    def readiness(self) -> dict[str, Any]:
        return {"provider": "synthetic", "ready": True, "synthetic": True,
                "production_ready": False, "reason": "synthetic_fixture_only"}
