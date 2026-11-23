"""Real simulator/panel projection with synthetic profiles and no live services."""
import copy
import json

import pytest

from app.foundation.config import Settings
from app.foundation.demo_story_catalog import load_catalog
from app.foundation.menu_content import build_menu_page
from app.foundation.stand import HardwareIntegrationStand
from test_panel_session_actions import PanelStand


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized", [False, True])
async def test_silent_root_three_projects_five_public_story_labels_without_visit_data(tmp_path, authorized):
    h = PanelStand(tmp_path, identity=authorized)
    try:
        if authorized:
            await h.ready()
        await h.control("start")
        await h.menu()
        h.state.display_name = "Синтетик"
        h.state.interests = ["PRIVATE_INTEREST_SENTINEL"]
        h.state.personal_story_seed = "PRIVATE_SEED_SENTINEL"
        await h.key(3)
        assert h.state.mode == "personal_story_theme"
        assert h.state.handset == "down" and h.state.silent_mode
        panel = h.stand.panel_snapshot()
        page = panel["canonical_state"]["scene_menu_descriptor"]
        titles = {entry["id"]: entry["title"] for entry in load_catalog()}
        expected = [{"slot": item["slot"], "title": titles[item["id"]]}
                    for item in h.state.scene_menu_descriptor["items"]]
        assert page == {"scene": "personal_story_theme", "items": expected,
                        "buttons": [1, 2, 3, 4, 5, 7]}
        assert len(page["items"]) == 5
        assert panel["session"]["enabled_buttons"] == [1, 2, 3, 4, 5, 7]
        assert [button["id"] for button in panel["simulator"]["devices"]["buttons"]
                if button["enabled"]] == [1, 2, 3, 4, 5, 7]
        public = json.dumps(panel, ensure_ascii=False)
        for private in ("Синтетик", "PRIVATE_INTEREST_SENTINEL", "PRIVATE_SEED_SENTINEL",
                        "personal_story_selection", "personal_story_seed", "display_name", "interests"):
            assert private not in public
        assert all(entry["text"] not in public for entry in h.state.personal_story_selection)
        await h.key(7)
        assert h.state.mode == "root_menu"
        assert "scene_menu_descriptor" not in h.stand.panel_snapshot()["canonical_state"]
    finally:
        await h.close()


def public_page():
    entries = load_catalog()[:5]
    return build_menu_page("personal_story_theme", "3-5",
        personal_story_items=[(entry["id"], entry["title"]) for entry in entries]).as_dict()


@pytest.mark.asyncio
@pytest.mark.parametrize("forged", ["private_title", "legacy_theme"])
async def test_story_projection_rejects_forged_titles_and_non_catalogue_ids(forged):
    stand = HardwareIntegrationStand(settings=Settings(profile="mock"), background_scheduler=False)
    try:
        page = public_page()
        page["spoken_text"] = "PRIVATE_CONTEXT_SENTINEL"
        page["items"][0]["private_context"] = "PRIVATE_CONTEXT_SENTINEL"
        stand.application.last_canonical_state = {"mode": "personal_story_theme",
            "scene_menu_descriptor": page, "personal_story_selection": copy.deepcopy(page["items"])}
        valid = stand.panel_snapshot()["canonical_state"]["scene_menu_descriptor"]
        assert len(valid["items"]) == 5
        assert all(set(item) == {"slot", "title"} for item in valid["items"])
        if forged == "private_title":
            page["items"][0]["title"] = "PRIVATE_CONTEXT_SENTINEL"
        else:
            page["items"][0].update(id="dinosaurs", title="Динозавры")
        panel = stand.panel_snapshot()
        assert "scene_menu_descriptor" not in panel["canonical_state"]
        assert "PRIVATE_CONTEXT_SENTINEL" not in json.dumps(panel, ensure_ascii=False)
        assert "personal_story_selection" not in panel["canonical_state"]
    finally:
        await stand.aclose()


@pytest.mark.asyncio
async def test_story_projection_fails_closed_when_public_catalogue_is_unavailable(monkeypatch):
    from app.foundation import demo_story_catalog
    stand = HardwareIntegrationStand(settings=Settings(profile="mock"), background_scheduler=False)
    try:
        stand.application.last_canonical_state = {"mode": "personal_story_theme",
            "scene_menu_descriptor": public_page()}
        def unavailable():
            raise ValueError("synthetic invalid catalogue")
        monkeypatch.setattr(demo_story_catalog, "load_catalog", unavailable)
        assert "scene_menu_descriptor" not in stand.panel_snapshot()["canonical_state"]
    finally:
        await stand.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["fixed_story_playing", "personal_story_playing", "story_speech_analysis",
                                  "story_pause_announcement", "story_paused", "story_resume_announcement"])
async def test_story_nodes_project_only_personal_or_verified_public_fixed_story_id(mode):
    stand = HardwareIntegrationStand(settings=Settings(profile="mock"), background_scheduler=False)
    try:
        verified = next(story_id for story_id, entry in stand.stories.items()
                        if entry.get("rights_status") in {"licensed", "public_domain", "owner_authored", "operator_approved"})
        for story_id in ("PERSONAL_STORY", verified):
            stand.application.last_canonical_state = {"mode": mode, "active_story_id": story_id,
                "personal_story_selection": [{"text": "PRIVATE_STORY_SENTINEL"}],
                "personal_story_seed": "PRIVATE_SEED_SENTINEL", "display_name": "PRIVATE_NAME_SENTINEL"}
            public = stand.panel_snapshot()["canonical_state"]
            assert public["active_story_id"] == story_id
            assert not any(key in public for key in ("personal_story_selection", "personal_story_seed", "display_name"))
            assert "PRIVATE_" not in json.dumps(public)
        stand.application.last_canonical_state["active_story_id"] = "PRIVATE_ID_SENTINEL"
        panel = stand.panel_snapshot()
        assert "active_story_id" not in panel["canonical_state"]
        assert "PRIVATE_" not in json.dumps(panel)
        stand.application.last_canonical_state["active_story_id"] = ["PRIVATE_ID_SENTINEL"]
        assert "active_story_id" not in stand.panel_snapshot()["canonical_state"]
    finally:
        await stand.aclose()


@pytest.mark.asyncio
async def test_story_id_is_not_projected_outside_story_nodes_or_from_unverified_placeholder():
    stand = HardwareIntegrationStand(settings=Settings(profile="mock"), background_scheduler=False)
    try:
        for mode in ("root_menu", "personal_story_theme", "personal_story_generating", "bear_dialogue"):
            stand.application.last_canonical_state = {"mode": mode, "active_story_id": "PERSONAL_STORY"}
            assert "active_story_id" not in stand.panel_snapshot()["canonical_state"]
        stand.stories["placeholder_story"] = {"development_only": True}
        stand.application.last_canonical_state = {"mode": "fixed_story_playing", "active_story_id": "placeholder_story"}
        assert "active_story_id" not in stand.panel_snapshot()["canonical_state"]
    finally:
        await stand.aclose()
