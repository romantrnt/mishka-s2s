"""Immutable chooser pages shared by routing, lights, narration and operator UI.

Only verified catalogue entries become choices. This module never invokes TTS,
changes session state, or treats a model's claims as a capability declaration.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .age_policy import policy_for_band

_BAND_ALIASES = {"4-5": "3-5", "6-7": "5-7"}
_BUTTON_WORDS = {1: "один", 2: "два", 3: "три", 4: "четыре", 5: "пять"}
_SCENES = frozenset({"fixed_story_menu", "game_menu", "personal_story_theme", "schedule_weather"})
# Exact existing canonical theme IDs, not a new generated-content whitelist.
STORY_THEME_ITEMS = (
    ("dinosaurs", "Динозавры"),
    ("космос", "Космос"),
    ("лес", "Лес"),
    ("дружелюбные машины", "Дружелюбные машины"),
    ("море", "Море"),
)


def normalized_age_band(band: str) -> str:
    """Map the existing exhibition labels to equivalent catalogue labels."""
    policy_for_band(band)  # Unknown labels must not silently pick an age policy.
    return _BAND_ALIASES.get(band, band)


@dataclass(frozen=True)
class MenuItem:
    slot: int
    id: str
    title: str

    def as_dict(self) -> dict[str, Any]:
        return {"slot": self.slot, "id": self.id, "title": self.title}


@dataclass(frozen=True)
class MenuPage:
    scene: str
    page: int
    page_count: int
    items: tuple[MenuItem, ...]
    buttons: tuple[int, ...]
    spoken_text: str

    def as_dict(self) -> dict[str, Any]:
        return {"scene": self.scene, "page": self.page,
                "page_count": self.page_count,
                "items": [item.as_dict() for item in self.items],
                "buttons": list(self.buttons), "spoken_text": self.spoken_text}

    def action_for(self, button: int) -> tuple[str, str | None] | None:
        """Resolve only controls advertised by this exact page."""
        if type(button) is not int or button not in self.buttons:
            return None
        # The game chooser has five stable activities; its fifth slot is play.
        for item in self.items:
            if item.slot == button:
                return "select", item.id
        if button == 5:
            return "help", None
        if button == 6:
            return "next_page", None
        if button == 7:
            return "back", None
        return None


def _label(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("menu_title_invalid")
    # Catalogue titles are data, not speech markup or hardware instructions.
    if any(character in value for character in "<>{}\x00"):
        raise ValueError("menu_title_invalid")
    return " ".join(value.split())


def _story_items(stories: Any, band: str) -> tuple[tuple[str, str], ...]:
    if stories is None:
        return ()
    catalog = getattr(stories, "stories", None)
    verify = getattr(stories, "get_verified", None)
    if not isinstance(catalog, Mapping) or not callable(verify):
        return ()
    selected = []
    for story_id in catalog:
        if not isinstance(story_id, str) or not story_id:
            continue
        item = verify(story_id)
        if not isinstance(item, Mapping) or item.get("availability") != "available":
            continue
        eligible = item.get("eligible_age_bands")
        if not isinstance(eligible, (list, tuple)):
            continue
        # Unknown eligibility labels do not make an entry eligible.
        if band not in {_BAND_ALIASES.get(value, value)
                        for value in eligible if isinstance(value, str)}:
            continue
        selected.append((story_id, _label(item.get("display_title"))))
    return tuple(selected)


def build_menu_page(scene: str, band: str, page: int = 0, *,
                    stories: Any = None,
                    game_specs: Iterable[Any] = (), personal_story_available: bool = True, day_items: Iterable[tuple[str,str]] = ()) -> MenuPage:
    """Build a bounded page; the game chooser owns slots 1..5.

    ``stories`` is the current FixedStoryManifest, ``game_specs`` comes from the
    actual implemented-game registry. Rendering callers still verify that owned
    speech is available; the descriptor itself promises only these choices.
    """
    if scene not in _SCENES:
        raise ValueError("menu_scene_invalid")
    if type(page) is not int or page < 0:
        raise ValueError("menu_page_invalid")
    policy = policy_for_band(band)
    normalized_band = normalized_age_band(band)
    if scene == "fixed_story_menu":
        choices = _story_items(stories, normalized_band)
    elif scene == "game_menu":
        choices = tuple((spec.game_id, _label(spec.display_title))
                        for spec in game_specs if getattr(spec, "available", False) is True)
        if any(not isinstance(item_id, str) or not item_id for item_id, _ in choices):
            raise ValueError("menu_item_invalid")
    elif scene == "schedule_weather":
        approved = {'date':'Дата','weather':'Погода','what_next':'Следующее занятие','where_now':'Текущее занятие'}
        choices = tuple((item_id, title) for item_id,title in day_items
                        if approved.get(item_id) == title)
    else:
        choices = STORY_THEME_ITEMS if personal_story_available else ()
    if len({item_id for item_id, _ in choices}) != len(choices):
        raise ValueError("menu_duplicate_item")
    size = 5 if scene == "game_menu" else min(4, policy.max_choices)
    count = max(1, (len(choices) + size - 1) // size)
    page %= count
    items = tuple(MenuItem(slot, item_id, title)
                  for slot, (item_id, title) in enumerate(choices[page * size:(page + 1) * size], 1))
    buttons = tuple(item.slot for item in items) + (() if scene == "game_menu" else (5,)) + ((6,) if count > 1 else ()) + (7,)
    parts = [f"Кнопка {_BUTTON_WORDS[item.slot]}: {item.title}." for item in items]
    if not items:
        parts.append("Сейчас здесь нет доступных вариантов.")
    if scene != "game_menu":
        parts.append("Пять — помощь.")
    else:
        parts.append("Можно сказать: не хочу играть.")
    if count > 1:
        parts.append("Шесть — следующие варианты.")
    parts.append("Семь — назад.")
    spoken_text = " ".join(parts)
    if len(spoken_text) > 320:
        # Never truncate a title or omit a control while routing still offers it.
        raise ValueError("menu_narration_too_long")
    return MenuPage(scene, page, count, items, buttons, spoken_text)
