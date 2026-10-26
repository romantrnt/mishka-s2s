"""Canonical age/accessibility policy; no age is guessed from identity or voice."""
from __future__ import annotations
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any, Mapping
from zoneinfo import ZoneInfo

@dataclass(frozen=True)
class AgePolicy:
    band: str
    source: str
    age_years: int | None
    max_words: int
    max_choices: int
    first_hint_after_seconds: int
    repeat_count: int
    dialogue_turn_limit: int
    game_response_window_ms: int
    def as_dict(self) -> dict[str, Any]: return asdict(self)

POLICIES = {
    "1-3": AgePolicy("1-3", "unknown", None, 8, 1, 20, 2, 4, 20000),
    "3-5": AgePolicy("3-5", "unknown", None, 18, 2, 18, 2, 5, 18000),
    "5-7": AgePolicy("5-7", "unknown", None, 30, 3, 15, 1, 6, 15000),
    # Exhibition onboarding uses the physical button labels 1-3, 4-5 and 6-7.
    # Keep historical profile-derived bands for non-exhibition callers.
    "4-5": AgePolicy("4-5", "unknown", None, 18, 2, 18, 2, 5, 18000),
    "6-7": AgePolicy("6-7", "unknown", None, 30, 3, 15, 1, 6, 15000),
}

def age_details(profile: Mapping[str, Any] | None, *, now: datetime | None = None) -> dict[str, Any]:
    value = profile or {}; raw = value.get("date_of_birth")
    result = {"age_years": None, "age_band": "3-5", "age_band_source": "anonymous_default" if not profile else "profile_missing_default", "profile_data_incomplete": bool(profile) and not isinstance(raw, str), "current_date": None, "timezone": str(value.get("timezone") or "Europe/Moscow")}
    try:
        zone = ZoneInfo(result["timezone"]); today = now.astimezone(zone).date() if now else datetime.now(zone).date(); result["current_date"] = today.isoformat()
        born = date.fromisoformat(raw) if isinstance(raw, str) else None
        if born is None or born > today:
            result["profile_data_incomplete"] = bool(profile)
            return result
        years = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
        result.update(age_years=years, age_band="1-3" if years <= 3 else "3-5" if years <= 5 else "5-7", age_band_source="profile_dob", profile_data_incomplete=False)
    except (TypeError, ValueError, KeyError):
        result["profile_data_incomplete"] = bool(profile)

    return result

def policy_for(profile: Mapping[str, Any] | None, *, now: datetime | None = None) -> AgePolicy:
    details = age_details(profile, now=now); base = POLICIES[details["age_band"]]
    return AgePolicy(base.band, details["age_band_source"], details["age_years"], base.max_words, base.max_choices, base.first_hint_after_seconds, base.repeat_count, base.dialogue_turn_limit, base.game_response_window_ms)

def policy_for_band(band: str, *, source: str = "unknown") -> AgePolicy:
    """Return the bounded policy selected by an exhibition age button."""
    base = POLICIES.get(str(band))
    if base is None:
        raise ValueError("unknown age band")
    return AgePolicy(base.band, source, None, base.max_words, base.max_choices,
                     base.first_hint_after_seconds, base.repeat_count,
                     base.dialogue_turn_limit, base.game_response_window_ms)

def profile_context(profile: Mapping[str, Any] | None, *, now: datetime | None = None) -> dict[str, Any]:
    value = profile or {}; details = age_details(profile, now=now); policy = policy_for(profile, now=now)
    return {"profile_ref": str(value.get("profile_ref") or value.get("child_id")) if value.get("profile_ref") or value.get("child_id") else None, "display_name": str(value["display_name"])[:80] if isinstance(value.get("display_name"), str) and value["display_name"].strip() else None, "language": str(value.get("language") or "ru-RU"), "timezone": details["timezone"], "age_years": details["age_years"], "age_band": details["age_band"], "age_band_source": details["age_band_source"], "profile_data_incomplete": details["profile_data_incomplete"], "current_date": details["current_date"], "accessibility": {"motor": list(value.get("motor_flags") or [])[:8], "sensory": list(value.get("sensory_flags") or [])[:8], "communication": list(value.get("communication_flags") or [])[:8]}, "age_policy": policy.as_dict()}


def trusted_canonical_age(state: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Use only a validated exhibition selection already owned by canonical state."""
    value = state or {}
    years, band, source = (value.get("age_years"), value.get("age_band"),
                           value.get("age_band_source"))
    if source not in {"exhibition_button", "exhibition_voice"}:
        return None
    if isinstance(years, bool) or not isinstance(years, int) or not 1 <= years <= 7:
        return None
    expected_band = "1-3" if years <= 3 else "4-5" if years <= 5 else "6-7"
    if band != expected_band:
        return None
    policy = policy_for_band(expected_band, source=source).as_dict()
    if value.get("age_policy") != policy:
        return None
    return {"age_years": years, "age_band": expected_band,
            "age_band_source": source, "age_policy": policy}
