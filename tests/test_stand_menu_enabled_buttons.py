"""Canonical menu actions remain reachable through the real WebPanel projection."""
import copy
import json
from datetime import datetime, timezone

import pytest

from app.foundation.canonical_contract import CanonicalState
from app.foundation.config import Settings
from app.foundation.max_json import MaxJsonBoundary
from app.foundation.remote_devices import RemoteDeviceSimulator
from app.foundation.schedule_weather import ProductionScheduleWeatherService
from app.foundation.simulator import SimulatorAcceptanceHarness
from test_dialogue_json_lifecycle import catalogue
from test_card_doctors_profile import doctor_card, NAME
from test_profile_onboarding import Client
from test_scene_menu_runtime import Render
from test_contextual_game_flow import ApprovedActivityRequests
from app.foundation.activity_requests import ActivityRequestGate


class MenuStand:
    def __init__(self, tmp_path, band="1-3", *, weather=True, identity=False):
        self.harness = SimulatorAcceptanceHarness(settings=Settings(
            profile="mock", hardware_mode="simulator", identity_mode="max_json" if identity else "guest_only"))
        self.stand = self.harness.stand
        self.app = self.stand.application
        self.app.bounded_s2s.activity_request_gate = ActivityRequestGate(ApprovedActivityRequests())
        if not identity:
            self.app.canonical_runtime.sessions["stand"] = CanonicalState(
                "stand", mode="root_menu", handset="up", age_band=band)
        self.app.canonical_runtime.stories = catalogue(tmp_path)
        # Synthetic audio is not an approved Telefon asset; use a safe fixture
        # title instead of relying on that exact production SHA exception.
        self.app.canonical_runtime.stories.stories['fixed_neznayka_v1']['display_title'] = 'Лесная сказка'
        self.app.bounded_s2s.tts = self.app.bounded_s2s.voice = Render()
        # Exercise actual owned rendering/leases without playing fixture audio.
        self.app.audio._private_available = lambda: True
        self.app.audio.play_pcm_owned = lambda handle, audio: None
        if not weather:
            self.app.schedule_weather = ProductionScheduleWeatherService(None)
        self.simulator = RemoteDeviceSimulator(self.stand, admits_users=lambda: True)
        self.stand.remote_devices = self.simulator

    @property
    def state(self):
        return self.app.canonical_runtime.sessions["stand"]

    async def edge(self, button, press="press"):
        return await self.simulator.action({"action": "button", "button_id": button, "press": press})

    async def key(self, button):
        result = await self.edge(button)
        await self.edge(button, "release")
        return result

    async def done(self, lease=None):
        lease = dict(lease or self.app._active_audio["stand"])
        return await self.harness.audio_completed(lease["content_id"], identity={
            **lease, "response_id": lease["content_id"]})

    def assert_menu_controls(self):
        expected = self.state.scene_menu_descriptor["buttons"]
        panel = self.stand.panel_snapshot()
        assert panel["session"]["enabled_buttons"] == expected
        assert [button["id"] for button in panel["simulator"]["devices"]["buttons"]
                if button["enabled"]] == expected
        assert panel["canonical_state"]["scene_menu_descriptor"]["buttons"] == expected
        return panel

    async def close(self):
        await self.stand.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("band", ["1-3", "3-5", "5-7"])
@pytest.mark.parametrize("slot", range(1, 6))
async def test_actual_five_story_controls_project_choices_and_select_verified_asset(tmp_path, band, slot):
    h = MenuStand(tmp_path, band)
    try:
        await h.key(2)
        assert h.state.mode == "fixed_story_menu" and h.state.scene_menu_phase == "narrating"
        page = h.state.scene_menu_descriptor
        assert page["page_count"] == 1 and page["page"] == 0
        assert page["buttons"] == [1, 2, 3, 4, 5, 7]
        assert {item["id"] for item in page["items"]} == set(h.app.canonical_runtime.stories.stories)
        h.assert_menu_controls()
        selected = page["items"][slot - 1]
        assert selected["slot"] == slot
        assert (await h.key(slot))["reason"] == "fixed_story_started"
        assert h.state.mode == "fixed_story_playing" and h.state.active_story_id == selected["id"]
        assert h.app._active_audio["stand"]["content_id"] == selected["id"]
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("band,weather,buttons", [
    ("1-3", True, [1, 6, 7]),
    ("3-5", True, [1, 2, 7]),
    ("5-7", True, [1, 2, 7]),
    ("1-3", False, [1, 7]),
])
async def test_actual_day_controls_follow_available_items_and_age_page(tmp_path, band, weather, buttons):
    h = MenuStand(tmp_path, band, weather=weather)
    try:
        await h.key(7)
        assert h.state.mode == "schedule_weather"
        h.assert_menu_controls()
        assert h.stand.panel_snapshot()["session"]["enabled_buttons"] == buttons
        if 6 in buttons:
            await h.key(6)
            assert h.state.scene_menu_descriptor["items"][0]["id"] == "weather"
            h.assert_menu_controls()
        # Day slots contain facts; back leads to the root help control.
        await h.key(7)
        assert h.state.mode == "root_menu"
        await h.key(5)
        assert h.state.mode == "wellbeing_check"
        assert h.stand.panel_snapshot()["session"]["enabled_buttons"] == [1, 5, 7]
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_page_edges_keep_hold_and_old_audio_guards_then_back_reset_and_new_entry(tmp_path):
    h = MenuStand(tmp_path)
    try:
        await h.key(7)  # The one-choice young day page genuinely advertises next.
        old = dict(h.app._active_audio["stand"])
        assert (await h.edge(6))["reason"] == "scene_menu_page"
        page, current = copy.deepcopy(h.state.scene_menu_descriptor), dict(h.app._active_audio["stand"])
        assert (await h.edge(6))["reason"] == "button_held"
        assert h.state.scene_menu_descriptor == page and h.app._active_audio["stand"] == current
        await h.done(old)
        assert h.state.scene_menu_descriptor == page and h.app._active_audio["stand"] == current
        h.assert_menu_controls()
        await h.edge(6, "release")
        await h.key(7)
        assert h.state.mode == "root_menu"
        assert h.stand.panel_snapshot()["session"]["enabled_buttons"] == list(range(1, 8))
        await h.key(2)
        h.assert_menu_controls()
        await h.simulator.action({"action": "reset"})
        panel = h.stand.panel_snapshot()
        assert panel["canonical_state"].get("mode") != "fixed_story_menu"
        assert "scene_menu_descriptor" not in panel["canonical_state"]
        assert panel["session"]["enabled_buttons"] == list(range(1, 8))
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_projection_reads_owned_committed_page_without_asset_io_and_ignores_foreign_copy(tmp_path):
    h = MenuStand(tmp_path)
    try:
        await h.key(2)
        owned = list(h.state.scene_menu_descriptor["buttons"])
        h.app.last_canonical_state["scene_menu_descriptor"] = {"scene": "fixed_story_menu", "buttons": [4]}
        h.app.canonical_runtime.stories.get_verified = lambda _: pytest.fail("snapshot must not reverify assets")
        assert h.stand.panel_snapshot()["session"]["enabled_buttons"] == owned
        h.state.scene_menu_active = "schedule_weather"  # Rejected/stale owner cannot grant old menu controls.
        assert h.stand.panel_snapshot()["session"]["enabled_buttons"] == []
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_day_capability_refresh_revokes_old_page_control_without_reusing_held_slot(tmp_path):
    h = MenuStand(tmp_path)
    try:
        await h.key(7)
        await h.key(6)
        assert h.state.scene_menu_descriptor["items"][0]["id"] == "weather"
        old = dict(h.app._active_audio["stand"])
        h.app.schedule_weather = ProductionScheduleWeatherService(None)
        await h.key(1)  # Refresh consumes this edge; it cannot become a date request.
        assert h.state.scene_menu_refresh_required
        assert h.state.scene_menu_descriptor["items"][0]["id"] == "date"
        assert h.stand.panel_snapshot()["session"]["enabled_buttons"] == [1, 7]
        assert h.state.active_audio_id == "MENU_DAY"
        current = dict(h.app._active_audio["stand"])
        await h.done(old)
        assert h.app._active_audio["stand"] == current and h.state.scene_menu_refresh_required
        h.assert_menu_controls()
        await h.done()
        h.assert_menu_controls()
        assert not h.state.scene_menu_refresh_required
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_actual_mapped_card_day_label_is_public_without_private_card_data(tmp_path):
    h = MenuStand(tmp_path, identity=True)
    client = Client(doctor_card())
    boundary = MaxJsonBoundary(h.app, client, session_id="stand",
        now=lambda: datetime(2029, 1, 1, tzinfo=timezone.utc))
    boundary.start(background=False)
    try:
        client.events.append({"kind": "patient_ready"})
        assert (await boundary.step())["reason"] == "profile_ready"
        await h.simulator.action({"action": "handset", "state": "up"})
        await h.harness.advance(5)
        await h.done()
        await h.done()
        await h.key(7)
        assert h.state.mode == 'schedule_weather'
        assert h.state.identity_capabilities == ["personalize"]
        assert h.app._canonical_profiles["stand"]["card_doctor_names"] == [NAME]
        assert any(item["id"] == "card_doctors" for item in h.state.scene_menu_descriptor["items"])
        panel = h.assert_menu_controls()
        public = json.dumps(panel, ensure_ascii=False)
        assert "Врачи в моей карточке" in public
        assert not any(value in public for value in (NAME, "UNUSED_PRIVATE_CODE", "card_doctor_names", "card_visit_date", "card_doctors_source"))
        owned = list(h.state.scene_menu_descriptor["buttons"])
        for mutation in ("foreign_id", "private_title", "private_field"):
            page = copy.deepcopy(h.state.scene_menu_descriptor)
            row = next(item for item in page["items"] if item["id"] == "card_doctors")
            if mutation == "foreign_id": row["id"] = "unverified_doctors"
            elif mutation == "private_title": row["title"] = NAME
            else: row["private_context"] = NAME
            h.app.last_canonical_state["scene_menu_descriptor"] = page
            forged = h.stand.panel_snapshot()
            assert forged["session"]["enabled_buttons"] == owned  # Authority is still the accepted canonical state.
            if mutation == "private_field":
                assert "scene_menu_descriptor" in forged["canonical_state"]
            else:
                assert "scene_menu_descriptor" not in forged["canonical_state"]
            assert NAME not in json.dumps(forged, ensure_ascii=False)
    finally:
        await boundary.close()
        await h.close()
