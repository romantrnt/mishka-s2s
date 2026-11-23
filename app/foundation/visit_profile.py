"""Normalized per-visit profile and unambiguous Russian name gender inference."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping

_EXPLICIT_GENDER_MAP = {
    "masculine": "masculine",
    "male": "masculine",
    "m": "masculine",
    "boy": "masculine",
    "м": "masculine",
    "муж": "masculine",
    "мужской": "masculine",
    "feminine": "feminine",
    "female": "feminine",
    "f": "feminine",
    "girl": "feminine",
    "ж": "feminine",
    "жен": "feminine",
    "женский": "feminine",
    "neutral": "neutral",
}

AMBIGUOUS_NAMES = frozenset({
    "саша", "женя", "валя", "шура", "лера", "толя", "ники", "алекс", "слава",
})

MASCULINE_NAMES = frozenset({
    "александр", "алексей", "алёша", "алеша", "анатолий", "андрей", "антон",
    "арсений", "арсен", "артем", "артём", "артемий", "артур", "богдан", "борис",
    "борька", "боря", "вадим", "валентин", "ваня", "василий", "виктор", "витя",
    "виталий", "влад", "владимир", "владислав", "вова", "володя", "всеволод",
    "вячеслав", "гавриил", "геннадий", "гена", "георгий", "герман", "глеб",
    "григорий", "гриша", "давид", "даниил", "данил", "данила", "даня", "демид",
    "денис", "дима", "дмитрий", "евгений", "егор", "елисей", "захар", "иван",
    "игорь", "илья", "илюша", "кирилл", "клим", "коля", "константин", "костя",
    "кузьма", "лев", "лёва", "лева", "леонид", "лёня", "леня", "макар",
    "максим", "макс", "марк", "матвей", "мирон", "мирослав", "михаил", "миша",
    "назар", "никита", "николай", "олег", "павел", "паша", "петр", "пётр",
    "петя", "платон", "прохор", "роман", "рома", "ростислав", "руслан",
    "савелий", "святослав", "семен", "семён", "сеня", "сергей", "сережа",
    "серёжа", "слава", "станислав", "стас", "степан", "степа", "стёпа",
    "тарас", "тимофей", "тима", "тимур", "тихон", "федор", "фёдор", "федя",
    "филипп", "эдгар", "эдуард", "юрий", "юра", "яков", "ярослав", "ярик",
    "яша",
})

FEMININE_NAMES = frozenset({
    "агата", "аглая", "аделина", "адель", "алана", "алевтина", "александра",
    "алена", "алёна", "алина", "алиса", "алла", "альбина", "амелия",
    "анастасия", "ангелина", "анжела", "анна", "аня", "антонина", "арина",
    "ася", "белла", "богдана", "валентина", "валерия", "варвара", "варя",
    "василиса", "вера", "вероника", "вика", "виктория", "виолетта", "влада",
    "галина", "галя", "дарина", "дарья", "даша", "диана", "дина", "ева",
    "евгения", "евдокия", "екатерина", "катя", "елена", "лена", "елизавета",
    "лиза", "есения", "жанна", "злата", "зоя", "инга", "инесса", "инна",
    "ирина", "ира", "карина", "каролина", "кира", "клавдия", "кристина",
    "ксения", "ксюша", "лада", "лариса", "лида", "лидия", "лилия", "лиля",
    "люба", "любовь", "люда", "людмила", "маргарита", "рита", "марина",
    "мария", "маша", "марья", "марта", "милана", "милена", "надежда", "надя",
    "наталья", "наталия", "наташа", "нелли", "ника", "нина", "нонна",
    "оксана", "олеся", "ольга", "оля", "полина", "поля", "раиса", "рая",
    "регина", "римма", "роза", "сабина", "света", "светлана", "серафима",
    "снежана", "софья", "софия", "соня", "стефания", "таисия", "тася",
    "тамара", "татьяна", "таня", "ульяна", "фаина", "эвелина", "элина",
    "эльвира", "эмма", "юлия", "юля", "яна", "ярослава",
})

_INTEREST_ALIASES = {
    "машины": "cars",
    "машинки": "cars",
    "авто": "cars",
    "автомобили": "cars",
    "гонки": "cars",
    "транспорт": "cars",
    "поезда": "cars",
    "самолеты": "cars",
    "самолёты": "cars",
    "cars": "cars",
    "vehicles": "cars",
    "friendly_machines": "cars",
    "динозавры": "dinosaurs",
    "динозаврики": "dinosaurs",
    "dinosaurs": "dinosaurs",
    "годзилла": "godzilla",
    "godzilla": "godzilla",
    "принцессы": "princesses",
    "принцесса": "princesses",
    "феи": "princesses",
    "замки": "princesses",
    "единороги": "princesses",
    "волшебство": "magic",
    "сказки": "magic",
    "magic": "magic",
    "princesses": "princesses",
    "космос": "space",
    "ракеты": "space",
    "звезды": "space",
    "звёзды": "space",
    "планеты": "space",
    "space": "space",
    "животные": "animals",
    "звери": "animals",
    "котики": "animals",
    "собаки": "animals",
    "animals": "animals",
    "лес": "forest",
    "природа": "forest",
    "forest": "forest",
    "море": "sea",
    "океан": "sea",
    "корабли": "sea",
    "рыбки": "sea",
    "sea": "sea",
}


def _norm_token(value: str) -> str:
    return value.strip().casefold().replace("ё", "е")


def normalize_gender(value: Any) -> str | None:
    return _EXPLICIT_GENDER_MAP.get(_norm_token(value)) if isinstance(value, str) else None


def infer_grammatical_gender(name: str | None = None, *, explicit_gender: str | None = None, name_confident: bool = True) -> str:
    """Return 'masculine', 'feminine', or 'neutral' from explicit card field or unambiguous name."""
    if isinstance(explicit_gender, str) and explicit_gender.strip():
        mapped = normalize_gender(explicit_gender)
        if mapped in {"masculine", "feminine", "neutral"}:
            return mapped
    if not name_confident or not isinstance(name, str) or not name.strip():
        return "neutral"
    first_word = name.strip()
    key = _norm_token(first_word)
    if not key or key in AMBIGUOUS_NAMES:
        return "neutral"
    in_masc = key in MASCULINE_NAMES or first_word.strip().casefold() in MASCULINE_NAMES
    in_fem = key in FEMININE_NAMES or first_word.strip().casefold() in FEMININE_NAMES
    if in_masc and not in_fem:
        return "masculine"
    if in_fem and not in_masc:
        return "feminine"
    return "neutral"


def normalize_interests(raw: Any) -> tuple[str, ...]:
    """Normalize a bounded list of interest tags without leaking raw text."""
    if not isinstance(raw, (list, tuple, set)):
        return ()
    out: list[str] = []
    for item in list(raw)[:12]:
        if not isinstance(item, str):
            continue
        cleaned = _norm_token(item)
        if not cleaned or len(cleaned) > 40:
            continue
        mapped = _INTEREST_ALIASES.get(cleaned, cleaned)
        if mapped not in out:
            out.append(mapped)
    return tuple(out)


def gendered_phrase(gender: str, *, masculine: str, feminine: str, neutral: str) -> str:
    """Pick natural Russian phrasing for the known grammatical gender or neutral fallback."""
    if gender == "masculine":
        return masculine
    if gender == "feminine":
        return feminine
    return neutral


@dataclass(frozen=True)
class VisitProfile:
    """Normalized per-visit profile used across scenarios during one visit."""
    profile_status: str = "anonymous"
    display_name: str | None = None
    age_years: int | None = None
    age_band: str = "3-5"
    age_band_source: str = "anonymous_default"
    grammatical_gender: str = "neutral"
    interests: tuple[str, ...] = ()
    motor_flags: tuple[str, ...] = ()
    sensory_flags: tuple[str, ...] = ()
    communication_flags: tuple[str, ...] = ()
    content_constraints: tuple[str, ...] = ()
    trigger_topics: tuple[str, ...] = ()
    wheelchair_access: bool | None = None
    participation_mode: str | None = None
    acquaintance_kind: str | None = None

    @property
    def has_reliable_name(self) -> bool:
        if not isinstance(self.display_name, str) or not self.display_name.strip():
            return False
        from .private_welcome import valid_name
        return valid_name(self.display_name)

    def to_prompt_dict(self) -> dict[str, Any]:
        """Return only safe normalized fields for scenario prompts (no UID/diagnosis/raw card)."""
        data: dict[str, Any] = {
            "profile_status": self.profile_status,
            "age_band": self.age_band,
            "age_band_source": self.age_band_source,
            "grammatical_gender": self.grammatical_gender,
        }
        if self.has_reliable_name:
            data["display_name"] = self.display_name
        if self.age_years is not None:
            data["age_years"] = self.age_years
        if self.interests:
            data["interests"] = list(self.interests)
        if self.motor_flags or self.sensory_flags or self.communication_flags:
            data["accessibility"] = list(self.motor_flags + self.sensory_flags + self.communication_flags)
        if self.content_constraints:
            data["content_constraints"] = list(self.content_constraints)
        if self.acquaintance_kind in {"first", "repeat"}:
            data["acquaintance_kind"] = self.acquaintance_kind
        return data

    @classmethod
    def from_state_and_profile(
        cls,
        state: Any,
        profile: Mapping[str, Any] | None = None,
        facts: Mapping[str, Any] | None = None,
    ) -> "VisitProfile":
        prof = profile or {}
        fct = facts or {}
        display_name = getattr(state, "display_name", None) or prof.get("display_name")
        if not isinstance(display_name, str) or not display_name.strip():
            display_name = None
        explicit_gender = prof.get("grammatical_gender") or getattr(state, "grammatical_gender", None)
        gender = infer_grammatical_gender(display_name, explicit_gender=explicit_gender)
        acc = getattr(state, "accessibility_policy", None) or {}
        motor = tuple(acc.get("motor") or prof.get("motor_flags") or ())
        sensory = tuple(acc.get("sensory") or prof.get("sensory_flags") or ())
        comm = tuple(acc.get("communication") or prof.get("communication_flags") or ())
        constraints = tuple(acc.get("content_constraints") or ())
        active_triggers = tuple(getattr(state, "active_trigger_topics", ())) + tuple(
            str(item.get("topic_id"))
            for item in (prof.get("trigger_topics") or ())
            if isinstance(item, Mapping) and item.get("status") == "active" and item.get("topic_id")
        )
        interests = normalize_interests(prof.get("interests") or getattr(state, "interests", ()))
        status = str(getattr(state, "profile_status", "anonymous") or "anonymous")
        acquaintance = fct.get("acquaintance") or getattr(state, "acquaintance_kind", None)
        if acquaintance not in {"first", "repeat"}:
            acquaintance = None
        return cls(
            profile_status=status,
            display_name=display_name,
            age_years=getattr(state, "age_years", None),
            age_band=str(getattr(state, "age_band", "3-5") or "3-5"),
            age_band_source=str(getattr(state, "age_band_source", "anonymous_default") or "anonymous_default"),
            grammatical_gender=gender,
            interests=interests,
            motor_flags=motor,
            sensory_flags=sensory,
            communication_flags=comm,
            content_constraints=constraints,
            trigger_topics=active_triggers,
            wheelchair_access=fct.get("wheelchair_access") if isinstance(fct.get("wheelchair_access"), bool) else None,
            participation_mode=fct.get("participation_mode") if isinstance(fct.get("participation_mode"), str) else None,
            acquaintance_kind=acquaintance,
        )
