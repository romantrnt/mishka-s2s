"""Explicit screen sessions use the real application and simulator boundary."""
import asyncio
from datetime import datetime, timezone

import pytest

from app.foundation.canonical_contract import CanonicalState
from app.foundation.max_json import MaxJsonBoundary
from test_card_doctors_profile import doctor_card
from test_profile_onboarding import Client
from test_stand_menu_enabled_buttons import MenuStand


class PanelStand(MenuStand):
    def __init__(self, tmp_path, *, identity=False):
        super().__init__(tmp_path, identity=identity)
        self.app.canonical_runtime.sessions.clear()
        self.client = None
        self.boundary = None
        if identity:
            self.client = Client(doctor_card())
            self.boundary = MaxJsonBoundary(self.app, self.client, session_id="stand",
                now=lambda: datetime(2029, 1, 1, tzinfo=timezone.utc))
            self.boundary.start(background=False)

    @property
    def state(self):
        return self.app.canonical_runtime.sessions.get("stand")

    def scope(self):
        sim = self.stand.panel_snapshot()["simulator"]
        return {"instance_id": sim["instance_id"], "epoch": sim["epoch"],
                "session_id": sim["session"]["session_id"]}

    async def control(self, action, scope=None):
        body = {"action": action, **(scope or self.scope())}
        if action == "skip_interview":
            body["state_version"] = self.state.state_version
        return await self.simulator.action(body)

    async def ready(self):
        self.client.value = doctor_card()
        self.client.events.append({"kind": "patient_ready"})
        return await self.boundary.step()

    async def menu(self):
        await self.harness.advance(5)
        await self.done()  # Real owned welcome completion; no fabricated slot.
        if self.state.profile_status != "identified":
            assert self.state.mode == "exhibition_introduction"
            await self.control("skip_interview")
        await self.done()
        await self.key(6)  # Existing tutorial skip, not an invented navigation key.
        assert self.state.mode == "menu_ready"

    async def close(self):
        if self.boundary is not None:
            await self.boundary.close()
        await super().close()


@pytest.mark.asyncio
async def test_ready_down_can_end_authorization_without_start_or_hook_change(tmp_path):
    h = PanelStand(tmp_path, identity=True)
    try:
        await h.ready()
        session = h.stand.panel_snapshot()["simulator"]["session"]
        assert session["session_actions"] == ["end", "start"]
        assert not session["interaction_active"] and h.state.handset == "down"
        await h.control("end")
        assert not h.state.interaction_open and h.state.handset == "down"
        assert h.state.identity_request_id is None and not h.app._canonical_profiles
        assert not h.app._active_audio and h.client.value is None
        h.client.events.append({"kind": "patient_ready"})  # Old source notification has no live card.
        assert (await h.boundary.step())["reason"] == "invalid_client_profile"
        assert not h.state.interaction_open and not h.app._active_audio
        await h.ready()  # A genuinely new fixture ready carries a fresh validated card.
        assert h.state.mode == "profile_ready" and not h.state.interaction_open
        assert not h.app._active_audio
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_explicit_start_down_opens_logical_session_without_lift(tmp_path):
    h = PanelStand(tmp_path)
    try:
        before = h.scope()
        result = await h.control("start", before)
        assert "error" not in result, result
        assert result["reason"] == "panel_session_started"
        assert h.state.mode == "voice_welcome" and h.state.handset == "down"
        assert h.state.interaction_route == "panel" and h.state.interaction_open
        assert h.scope() == before
        assert not h.app.sessions.get("stand").handset_up
        assert h.stand.panel_snapshot()["simulator"]["session"]["session_actions"] == ["end"]
        version = h.state.state_version
        assert (await h.control("start"))["reason"] == "interaction_already_started"
        assert h.state.state_version == version
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_end_at_up_retains_real_hook_but_fresh_ready_waits_for_new_start(tmp_path):
    h = PanelStand(tmp_path, identity=True)
    try:
        await h.simulator.action({"action": "handset", "state": "up"})
        await h.ready()
        old = h.state.identity_request_id
        before = h.scope()
        assert (await h.control("end"))["reason"] == "soft_end"
        assert h.state.handset == "up" and h.app.sessions.get("stand").handset_up
        assert not h.state.interaction_open and h.state.interaction_route is None
        assert not h.app._active_audio and not h.app._canonical_profiles
        assert not h.app.conversation_contexts
        assert (await h.control("end", before))["error"]["code"] == "stale_simulator_epoch"
        assert (await h.ready())["reason"] == "profile_ready"
        assert h.state.identity_request_id != old and h.state.mode == "profile_ready"
        assert h.state.handset == "up" and not h.state.interaction_open
        assert not h.app._active_audio
        assert (await h.control("start"))["reason"] == "panel_session_started"
        assert h.state.interaction_route == "panel" and h.state.mode == "voice_welcome"
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", [False, True])
@pytest.mark.parametrize("ready_first", [False, True])
@pytest.mark.parametrize("physical_up", [False, True])
async def test_panel_down_auth_start_orders_and_ordinary_nonvoice_day(tmp_path, identity, ready_first, physical_up):
    h = PanelStand(tmp_path, identity=identity)
    try:
        if identity and ready_first:
            await h.ready()
        if physical_up:
            await h.simulator.action({"action": "handset", "state": "up"})
        await h.control("start")
        if identity and not ready_first:
            old = h.state
            await h.ready()
            assert h.state is not old  # Actual fresh-context auth barrier.
        assert h.state.handset == ("up" if physical_up else "down")
        assert h.state.interaction_route == ("handset" if physical_up else "panel")
        if identity:
            assert h.app._identity_scope_live("stand", "personalize")
        await h.menu()
        await h.key(7)
        assert h.state.mode == "schedule_weather"
        assert h.state.active_audio_id == "MENU_DAY"
        await h.key(7)
        assert h.state.mode == "root_menu" and h.state.handset == ("up" if physical_up else "down")
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("epoch", True), ("epoch", -1),
    ("instance_id", "old"), ("session_id", "foreign"), ("route", "panel"),
    ("handset", "up"), ("profile", {"name": "PRIVATE"})])
async def test_session_action_scope_is_closed_and_cannot_override_state(tmp_path, field, value):
    h = PanelStand(tmp_path)
    try:
        scope = h.scope()
        result = await h.simulator.action({"action": "start", **scope, field: value})
        assert result["error"]["code"] in {"validation_error", "stale_simulator_epoch"}
        assert h.state is None and not h.app._active_audio and h.scope() == scope
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_start_maintenance_fault_and_end_cleanup_admission(tmp_path):
    h = PanelStand(tmp_path)
    try:
        release_maintenance = h.app.acquire_runtime_maintenance()
        try:
            assert (await h.control("start"))["reason"] == "runtime_unavailable"
            assert "start" not in h.stand.panel_snapshot()["simulator"]["session"]["session_actions"]
        finally:
            release_maintenance()
        h.stand._capture_hid_fault = "synthetic"
        assert (await h.control("start"))["error"]["code"] == "runtime_unavailable"
        h.stand._capture_hid_fault = None
        await h.control("start")
        h.simulator.admits_users = lambda: False
        assert (await h.control("end"))["reason"] == "soft_end"
        assert not h.state.interaction_open and not h.app._active_audio
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_panel_real_edges_duplicate_down_and_lift_do_not_restart_or_cancel_owner(tmp_path):
    h = PanelStand(tmp_path)
    try:
        await h.control("start")
        original = dict(h.app._active_audio["stand"])
        scope = h.scope()
        version = h.state.state_version
        assert (await h.simulator.action({"action": "handset", "state": "down"}))["reason"] == "handset_already_placed"
        assert h.scope() == scope and h.app._active_audio["stand"] == original
        assert (await h.simulator.action({"action": "handset", "state": "up"}))["reason"] == "panel_handset_lifted"
        assert h.state.state_version == version and h.state.interaction_route == "panel"
        assert h.app.sessions.get("stand").handset_up
        assert h.app._active_audio["stand"] == original
        await h.simulator.action({"action": "handset", "state": "down"})
        assert h.state is None  # Existing exhibition reset fully retires the canonical context.
        assert not h.app.sessions.get("stand") or not h.app.sessions.get("stand").handset_up
        assert not h.app._active_audio and h.scope()["epoch"] > scope["epoch"]
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_end_cancels_start_waiting_at_actual_application_await(tmp_path):
    h = PanelStand(tmp_path, identity=True)
    entered, release = asyncio.Event(), asyncio.Event()
    original = h.app.expire_identity_leases
    pending = None
    async def expiry():
        if asyncio.current_task() is pending:
            entered.set()
            await release.wait()
        return await original()
    h.app.expire_identity_leases = expiry
    try:
        scope = h.scope()
        pending = asyncio.create_task(h.control("start", scope))
        await asyncio.wait_for(entered.wait(), 2)
        await h.control("end", scope)
        release.set()
        assert (await pending)["reason"] == "stale_session_generation"
        assert not h.state.interaction_open and not h.app._active_audio
        assert (await h.control("start"))["reason"] == "panel_session_started"
    finally:
        release.set()
        if pending:
            await asyncio.gather(pending, return_exceptions=True)
        await h.close()
