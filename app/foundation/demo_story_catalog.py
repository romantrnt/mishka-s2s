"""Authored local demo stories; visit data is used only for private selection/rendering."""
from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any

from .visit_profile import VisitProfile, normalize_interests


_CATALOG_PATH = Path(__file__).resolve().parents[2] / "content/stories/demo_catalog.json"
_CATEGORIES = {"adventure", "fairy", "neutral"}
_UNIVERSAL_CONSTRAINTS = {
    "avoid_parent_assumptions", "avoid_body_assumptions", "adult_neutral",
    "parent_absence", "limb_absence_or_difference", "separation", "loss",
    "medical_procedure_fear", "body_vulnerability", "bullying",
}
_THEME_CONSTRAINTS = {
    "animal_fear": {"animals", "dinosaurs", "godzilla"},
    "darkness_fear": {"night", "space"},
    "loud_sound_fear": {"music", "loud_sounds"},
    "quiet": {"music", "loud_sounds"},
    "avoid_loud_sounds": {"music", "loud_sounds"},
    "no_loud_sounds": {"music", "loud_sounds"},
}
_NAME_FORM = re.compile(r"[A-Za-zА-Яа-яЁё]{2,40}(?:[-'’][A-Za-zА-Яа-яЁё]{2,40})?")


def load_catalog() -> list[dict[str, Any]]:
    """Read fresh copies of the 30 public, authored entries; never write profiles here."""
    entries = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
    if not isinstance(entries, list) or len(entries) != 30:
        raise ValueError("Demo story catalogue must contain 30 entries")
    ids: set[str] = set()
    titles: set[str] = set()
    texts: set[str] = set()
    young_texts: set[str] = set()
    counts = dict.fromkeys(_CATEGORIES, 0)
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"id", "title", "category", "text", "young_text", "tags"}:
            raise ValueError("Invalid demo story entry")
        if any(not isinstance(entry[key], str) or not entry[key].strip()
               for key in ("id", "title", "category", "text", "young_text")):
            raise ValueError("Invalid demo story field")
        category = entry["category"]
        tags = entry["tags"]
        if category not in counts or not isinstance(tags, list) or not tags:
            raise ValueError("Invalid demo story category or tags")
        if any(not isinstance(tag, str) or not re.fullmatch(r"[a-z_]+", tag) for tag in tags):
            raise ValueError("Invalid demo story tag")
        if (entry["id"] in ids or entry["title"] in titles or entry["text"] in texts
                or entry["young_text"] in young_texts):
            raise ValueError("Duplicate demo story")
        for key in ("text", "young_text"):
            if (entry[key].count("{name}") > 1
                    or re.search(r"[{}]", entry[key].replace("{name}", ""))):
                raise ValueError("Unsupported demo story slot")
        young = entry["young_text"].replace("{name}", "друг")
        if (not 35 <= len(re.findall(r"[А-Яа-яЁё]+", young)) <= 65
                or "?" in young
                or any(len(re.findall(r"[А-Яа-яЁё]+", sentence)) > 8
                       for sentence in re.split(r"[.!?]+", young))):
            raise ValueError("Invalid young demo story bounds")
        ids.add(entry["id"])
        titles.add(entry["title"])
        texts.add(entry["text"])
        young_texts.add(entry["young_text"])
        counts[category] += 1
    if set(counts.values()) != {10}:
        raise ValueError("Demo story categories must contain 10 entries each")
    return entries


def _token(value: str) -> str:
    return value.strip().casefold().replace("ё", "е").replace("-", "_").replace(" ", "_")


def _allowed_entries(entries: list[dict[str, Any]], profile: VisitProfile) -> list[dict[str, Any]]:
    blocked: set[str] = set()
    unknown = False
    themes = {tag for entry in entries for tag in entry["tags"]} - {"gentle", "fallback"}
    for value in profile.content_constraints + profile.trigger_topics + profile.sensory_flags:
        if not isinstance(value, str) or not value.strip():
            continue
        token = _token(value)
        if token in _UNIVERSAL_CONSTRAINTS:
            continue
        if token in _THEME_CONSTRAINTS:
            blocked.update(_THEME_CONSTRAINTS[token])
            continue
        theme = re.sub(r"^(?:avoid_|no_)", "", token)
        normalized = normalize_interests((theme,))
        theme = normalized[0] if normalized else theme
        if theme in themes:
            blocked.add(theme)
        else:
            # An unknown preference is not a diagnosis; use the authored quiet core.
            unknown = True
    return [entry for entry in entries
            if not blocked.intersection(entry["tags"])
            and (not unknown or "fallback" in entry["tags"])]


def select_stories(profile: VisitProfile, *, seed: str) -> list[dict[str, Any]]:
    """Choose five different stories, stably per seed, interests first and then variety.

    Gender never filters or ranks a story. Tags remain public authored metadata;
    neither the name nor any private profile field is added to returned entries.
    Ages 1–3 receive the authored short retelling without changing the selection.
    """
    entries = _allowed_entries(load_catalog(), profile)
    interests = set(normalize_interests(profile.interests))
    selected: list[dict[str, Any]] = []
    category_counts = dict.fromkeys(_CATEGORIES, 0)
    while entries and len(selected) < 5:
        entry = min(entries, key=lambda item: (
            -len(interests.intersection(item["tags"])),
            category_counts[item["category"]],
            sha256((str(seed) + "\0" + item["id"]).encode("utf-8")).digest(),
        ))
        selected.append({**entry, "text": entry["young_text"] if profile.age_band == "1-3" else entry["text"]})
        category_counts[entry["category"]] += 1
        entries.remove(entry)
    return selected


def render_story(entry: Mapping[str, Any], profile: VisitProfile) -> str:
    """Render only the nominative name slot; unknown/unsafe names become 'друг'."""
    name = profile.display_name
    reliable = (
        isinstance(name, str)
        and _NAME_FORM.fullmatch(name) is not None
        and not (re.search(r"[A-Za-z]", name) and re.search(r"[А-Яа-яЁё]", name))
        and profile.has_reliable_name
    )
    replacement = name if reliable else "друг"
    text = entry.get("text", "")
    if not isinstance(text, str):
        raise ValueError("Demo story text must be a string")
    if re.search(r"[{}]", text.replace("{name}", "")):
        raise ValueError("Unsupported demo story slot")
    return text.replace("{name}", replacement)
