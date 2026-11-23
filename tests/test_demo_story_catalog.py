from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import replace
import json
import re

import pytest

from app.foundation import demo_story_catalog as catalog
from app.foundation.visit_profile import VisitProfile


def test_catalog_has_thirty_complete_distinct_authored_stories():
    entries = catalog.load_catalog()
    assert Counter(entry["category"] for entry in entries) == {
        "adventure": 10, "fairy": 10, "neutral": 10,
    }
    for field in ("id", "title", "text", "young_text"):
        assert len({entry[field] for entry in entries}) == 30
    for entry in entries:
        assert set(entry) == {"id", "title", "category", "text", "young_text", "tags"}
        assert 90 <= len(re.findall(r"[А-Яа-яЁё]+", entry["text"])) <= 150
        assert len(entry["text"].split("\n\n")) == 3
        assert "?" not in entry["text"]
        assert entry["text"].count("{name}") <= 1
        assert isinstance(entry["tags"], list) and entry["tags"]
    assert any("{name}" in entry["text"] for entry in entries)
    assert any("{name}" not in entry["text"] for entry in entries)


def test_all_thirty_young_retellings_have_short_complete_sentences():
    for entry in catalog.load_catalog():
        text = entry["young_text"]
        assert text != entry["text"]
        assert 35 <= len(re.findall(r"[А-Яа-яЁё]+", text.replace("{name}", "друг"))) <= 65
        assert len(text.split("\n\n")) == 2
        assert "?" not in text and text.count("{name}") <= 1
        for name in (None, "Саша", "Анна-Мария"):
            rendered = catalog.render_story({**entry, "text": text}, VisitProfile(display_name=name))
            assert "{" not in rendered and "}" not in rendered
            sentences = [sentence for sentence in re.split(r"[.!?]+", rendered) if re.search(r"[А-Яа-яЁё]", sentence)]
            assert len(sentences) >= 5
            assert all(1 <= len(re.findall(r"[А-Яа-яЁё]+", sentence)) <= 8 for sentence in sentences)


def test_all_base_stories_keep_the_older_sentence_and_volume_bounds():
    from app.foundation.age_policy import POLICIES

    for entry in catalog.load_catalog():
        text = catalog.render_story(entry, VisitProfile())
        assert 90 <= len(re.findall(r"[А-Яа-яЁё]+", text)) <= 150
        assert all(len(re.findall(r"[А-Яа-яЁё]+", sentence)) <= POLICIES["3-5"].max_words
                   for sentence in re.split(r"[.!?]+", text))


@pytest.mark.parametrize("band", ["3-5", "4-5", "5-7", "6-7"])
def test_age_changes_only_selected_text_and_preserves_selection_and_older_base(band):
    entries = {entry["id"]: entry for entry in catalog.load_catalog()}
    profile = VisitProfile(age_band=band, interests=("cars", "magic"))
    older = catalog.select_stories(profile, seed="same-visit-age")
    young = catalog.select_stories(replace(profile, age_band="1-3"), seed="same-visit-age")
    assert [entry["id"] for entry in young] == [entry["id"] for entry in older]
    for base_entry, young_entry in zip(older, young):
        source = entries[base_entry["id"]]
        assert base_entry["text"] == source["text"]
        assert young_entry["text"] == source["young_text"]
        for field in ("id", "title", "category", "tags"):
            assert base_entry[field] == young_entry[field] == source[field]
        assert source["text"] != young_entry["text"]
    assert young == catalog.select_stories(replace(profile, age_band="1-3"), seed="same-visit-age")


def test_public_catalog_has_only_fictional_metadata_not_visit_data():
    profile = VisitProfile(display_name="Людмила", profile_status="identified")
    before = catalog.load_catalog()
    selected = catalog.select_stories(profile, seed="synthetic-visit")
    for entry in selected:
        catalog.render_story(entry, profile)
    assert catalog.load_catalog() == before
    public = json.dumps(before, ensure_ascii=False)
    assert "Людмила" not in public
    for private_key in ("display_name", "profile_status", "profile_ref", "diagnosis", "uid"):
        assert private_key not in public


def test_all_authored_templates_pass_actual_trusted_text_validator():
    from app.foundation.validation import AsyncResponseValidator

    async def run():
        validator = AsyncResponseValidator()
        for entry in catalog.load_catalog():
            for variant in ("text", "young_text"):
                decision = await validator.validate_trusted(entry[variant].replace("{name}", "друг"))
                assert decision.allowed, (entry["id"], variant, decision.reason)
                assert decision.source == "regex_trusted"

    asyncio.run(run())


def test_selection_is_stable_and_contains_five_different_entries():
    profile = VisitProfile()
    first = catalog.select_stories(profile, seed="one-visit")
    assert first == catalog.select_stories(profile, seed="one-visit")
    assert len(first) == len({entry["id"] for entry in first}) == 5
    assert set(Counter(entry["category"] for entry in first).values()) <= {1, 2}
    orders = {
        tuple(entry["id"] for entry in catalog.select_stories(profile, seed=str(seed)))
        for seed in range(10)
    }
    assert len(orders) > 1


@pytest.mark.parametrize("band", ["1-3", "3-5"])
def test_results_are_fresh_and_caller_mutation_cannot_change_catalog(band):
    first = catalog.select_stories(VisitProfile(age_band=band), seed="mutable-result")
    original = first[0]["text"]
    first[0]["text"] = "changed"
    first[0]["tags"].append("changed")
    again = catalog.select_stories(VisitProfile(age_band=band), seed="mutable-result")
    assert again[0]["text"] == original
    assert "changed" not in again[0]["tags"]


@pytest.mark.parametrize("interest,tag,available", [
    ("машинки", "cars", 4),
    ("динозавры", "dinosaurs", 3),
    ("Годзилла", "godzilla", 3),
    ("принцессы", "princesses", 10),
    ("Единороги", "princesses", 10),
])
def test_normalized_interests_rank_before_variety_and_gender(interest, tag, available):
    profile = VisitProfile(grammatical_gender="feminine", interests=(interest,))
    selected = catalog.select_stories(profile, seed="interest-first")
    assert all(tag in entry["tags"] for entry in selected[:min(5, available)])
    assert selected == catalog.select_stories(
        replace(profile, grammatical_gender="masculine"), seed="interest-first")


@pytest.mark.parametrize("gender", ["masculine", "feminine", "neutral"])
def test_every_category_is_accessible_for_every_gender(gender):
    selected = catalog.select_stories(VisitProfile(grammatical_gender=gender), seed="all")
    assert {entry["category"] for entry in selected} == {"adventure", "fairy", "neutral"}


@pytest.mark.parametrize("name", ["Миша", "Аня", "Саша", "Женя", "Валя", "Алия", "Анна-Мария"])
def test_safe_names_including_ambiguous_gender_are_used_without_declension(name):
    profile = VisitProfile(display_name=name)
    for entry in catalog.load_catalog():
        rendered = catalog.render_story(entry, profile)
        assert "{" not in rendered and "}" not in rendered and "?" not in rendered
        if "{name}" in entry["text"]:
            assert name in rendered
        else:
            assert rendered == entry["text"]


@pytest.mark.parametrize("name", [
    None, "", "  Миша", "Миша  ", "Скажи", "Ignore", "Assistant", "Саsа",
    "{name}", "Миша?", "Анна\nВыполни", "Миша <script>", "https://example.test",
    "Игнорируй инструкции", "Саша Женя", "А" * 81,
])
def test_missing_suspicious_or_structurally_ambiguous_names_use_friend(name):
    entry = next(entry for entry in catalog.load_catalog() if "{name}" in entry["text"])
    rendered = catalog.render_story(entry, VisitProfile(display_name=name))
    assert rendered == entry["text"].replace("{name}", "друг")
    assert "{" not in rendered and "}" not in rendered


@pytest.mark.parametrize("trigger,forbidden", [
    ("animal_fear", {"animals", "dinosaurs", "godzilla"}),
    ("darkness_fear", {"night", "space"}),
    ("loud_sound_fear", {"music", "loud_sounds"}),
])
def test_trigger_constraints_override_even_matching_interests(trigger, forbidden):
    profile = VisitProfile(
        trigger_topics=(trigger,),
        interests=("animals", "dinosaurs", "godzilla", "space", "music"),
    )
    for seed in ("first", "second", "repeat"):
        selected = catalog.select_stories(profile, seed=seed)
        assert len(selected) == len({entry["id"] for entry in selected}) == 5
        assert all(not forbidden.intersection(entry["tags"]) for entry in selected)


@pytest.mark.parametrize("constraint", ["avoid_cars", "no_cars", "машины"])
def test_explicit_theme_constraints_are_normalized(constraint):
    profile = VisitProfile(interests=("cars",), content_constraints=(constraint,))
    selected = catalog.select_stories(profile, seed="blocked-interest")
    assert len(selected) == 5
    assert all("cars" not in entry["tags"] for entry in selected)


def test_combined_constraints_keep_five_safe_neutral_fallbacks():
    profile = VisitProfile(
        interests=("dinosaurs", "princesses", "space"),
        content_constraints=("avoid_parent_assumptions", "avoid_body_assumptions", "custom_topic"),
        trigger_topics=("animal_fear", "darkness_fear", "loud_sound_fear", "parent_absence"),
        sensory_flags=("quiet",),
    )
    selected = catalog.select_stories(profile, seed="many-constraints")
    assert len(selected) == len({entry["id"] for entry in selected}) == 5
    assert all(entry["category"] == "neutral" and "fallback" in entry["tags"] for entry in selected)
    assert all(set(entry["tags"]) == {"gentle", "fallback"} for entry in selected)
    for entry in selected:
        text = catalog.render_story(entry, profile)
        assert not re.search(r"\b(?:мама|папа|родител\w*|встань|побеги|прыгай)\b", text, re.I)


def test_accessibility_flags_do_not_invent_diagnoses_or_remove_interests():
    plain = VisitProfile(interests=("princesses",))
    adapted = replace(plain, motor_flags=("wheelchair",), communication_flags=("no_speech",),
                      wheelchair_access=True, participation_mode="buttons")
    assert catalog.select_stories(plain, seed="same") == catalog.select_stories(adapted, seed="same")


@pytest.mark.parametrize("bad_text", ["Привет, {display_name}.", "Привет, {name", "Привет, {{name}}."])
def test_renderer_rejects_unsupported_slots_instead_of_leaking_placeholders(bad_text):
    with pytest.raises(ValueError, match="slot"):
        catalog.render_story({"text": bad_text}, VisitProfile())


def test_loader_rejects_a_corrupted_local_catalog(tmp_path, monkeypatch):
    entries = catalog.load_catalog()
    entries[1]["id"] = entries[0]["id"]
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(catalog, "_CATALOG_PATH", path)
    with pytest.raises(ValueError, match="Duplicate"):
        catalog.load_catalog()


@pytest.mark.parametrize("corruption", ["question", "long_sentence", "missing_text"])
def test_loader_rejects_invalid_young_retellings(tmp_path, monkeypatch, corruption):
    entries = catalog.load_catalog()
    if corruption == "question":
        entries[0]["young_text"] = entries[0]["young_text"].replace(".", "?", 1)
    elif corruption == "long_sentence":
        entries[0]["young_text"] = (
            "Однажды добрый маленький герой придумал новую красивую картину для уютной мастерской. "
            "Он добавил простой узор. Рядом возник ещё один узор. "
            "Картинка стала очень красивой. Герой поставил её на большую полку. "
            "Теперь мастерская выглядела особенно уютной."
        )
    else:
        entries[0]["young_text"] = None
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(catalog, "_CATALOG_PATH", path)
    with pytest.raises(ValueError):
        catalog.load_catalog()
