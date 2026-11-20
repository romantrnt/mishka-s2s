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
        if self.state.mode == "menu_ready":
            return
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
@pytest.mark.parametrize("physical_up", [False, True])
async def test_explicit_end_after_local_help_allows_fresh_auth_without_reactivation(tmp_path, physical_up):
    h = PanelStand(tmp_path, identity=True)
    try:
        await h.control("start")
        await h.menu()
        if physical_up:
            await h.simulator.action({"action": "handset", "state": "up"})
        await h.key(5)
        if not physical_up:
            assert h.state.mode == "awaiting_handset" and h.state.pending_mode == "wellbeing_check"
            await h.simulator.action({"action": "handset", "state": "up"})
        await h.key(7)
        assert h.state.mode == "safety_stop" and h.state.active_audio_id == "PHRASE_091"
        await h.control("end")
        assert not h.state.interaction_open and not h.app._active_audio
        assert (await h.ready())["reason"] == "profile_ready"
        assert h.state.mode == "profile_ready" and not h.state.interaction_open
        assert h.state.handset == "up" and not h.app._active_audio
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("age_step", [False, True])
async def test_optional_guest_skip_is_scoped_and_late_old_audio_cannot_advance_it(tmp_path, age_step):
    h = PanelStand(tmp_path)
    try:
        await h.simulator.action({"action": "handset", "state": "up"})
        await h.harness.advance(5)
        await h.done()
        assert h.state.mode == "exhibition_introduction"
        if age_step:
            await h.done()
            await h.done()
            assert h.state.mode == "exhibition_name_capture"
            await h.stand.inject("text", {"text": "Тестик"})
            assert h.state.mode == "exhibition_age_prompt"
        old = dict(h.app._active_audio["stand"])
        payload = {"action": "skip_interview", **h.scope(), "state_version": h.state.state_version}
        await h.done()
        current = dict(h.app._active_audio["stand"])
        assert (await h.simulator.action(payload))["reason"] == "stale_state_version"
        assert h.app._active_audio["stand"] == current
        assert "skip_interview" in h.stand.panel_snapshot()["simulator"]["session"]["session_actions"]
        nonce = h.app._engagement_owners["stand"]["nonce"]
        assert (await h.control("skip_interview"))["reason"] == "exhibition_interview_skipped"
        assert h.state.mode == "exhibition_menu_tutorial_prompt" and h.state.age_years is None
        assert h.app._engagement_owners["stand"]["nonce"] != nonce
        await h.done(old)
        assert h.state.mode == "exhibition_menu_tutorial_prompt"
        assert "skip_interview" not in h.stand.panel_snapshot()["simulator"]["session"]["session_actions"]
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_explicit_start_down_opens_logical_session_without_lift(tmp_path):
    h = PanelStand(tmp_path)
    try:
        before = h.scope()
        result = await h.control("start", before)
        assert "error" not in result, result
        assert result["reason"] == "panel_silent_session_started"
        assert h.state.mode == "menu_ready" and h.state.handset == "down" and h.state.silent_mode
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
        if not physical_up:
            assert h.state.mode == "awaiting_handset" and h.state.pending_mode == "schedule_weather"
            await h.simulator.action({"action": "handset", "state": "up"})
        assert h.state.mode == "schedule_weather"
        assert h.state.active_audio_id == "MENU_DAY"
        await h.harness.advance(0.7)
        await h.key(7)
        assert h.state.mode == "root_menu" and h.state.handset == "up"
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("epoch", True), ("epoch", -1),
    ("instance_id", "old"), ("session_id", "foreign"), ("route", "panel"),
    ("handset", "up"), ("profile", {"name": "PRIVATE"}), ("action", ["start"]),
    ("action", {"start": True})])
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
        assert h.state.silent_mode and not h.app._active_audio
        scope = h.scope()
        version = h.state.state_version
        assert (await h.simulator.action({"action": "handset", "state": "down"}))["reason"] == "handset_already_placed"
        assert h.scope() == scope and h.state.state_version == version and h.state.silent_mode
        assert (await h.simulator.action({"action": "handset", "state": "up"}))["reason"] == "panel_handset_lifted"
        assert h.state.interaction_route == "panel" and not h.state.silent_mode
        assert h.app.sessions.get("stand").handset_up
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
        assert (await h.control("start"))["reason"] == "panel_silent_session_started"
    finally:
        release.set()
        if pending:
            await asyncio.gather(pending, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
async def test_new_start_waits_for_owned_end_cleanup_before_new_audio(tmp_path):
    h = PanelStand(tmp_path)
    original = h.app._cleanup_session_resources
    entered, release = asyncio.Event(), asyncio.Event()
    ending = starting = None
    async def cleanup(*args, **kwargs):
        result = await original(*args, **kwargs)
        if asyncio.current_task() is ending:
            entered.set()
            await release.wait()
        return result
    try:
        await h.control("start")
        h.app._cleanup_session_resources = cleanup
        ending = asyncio.create_task(h.control("end"))
        await asyncio.wait_for(entered.wait(), 2)
        starting = asyncio.create_task(h.control("start"))
        await asyncio.sleep(0)
        assert not starting.done() and not h.app._active_audio and not h.state.interaction_open
        release.set()
        assert (await ending)["reason"] == "soft_end"
        assert (await starting)["reason"] == "panel_silent_session_started"
        assert h.state.interaction_open and h.state.silent_mode and h.state.mode == "menu_ready"
    finally:
        release.set()
        await asyncio.gather(*(t for t in (ending, starting) if t), return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
async def test_ended_up_duplicate_lift_during_fresh_auth_cleanup_never_reactivates(tmp_path):
    h = PanelStand(tmp_path, identity=True)
    original = h.app._cleanup_session_resources
    entered, release = asyncio.Event(), asyncio.Event()
    pending = None
    async def cleanup(*args, **kwargs):
        result = await original(*args, **kwargs)
        if asyncio.current_task() is pending:
            entered.set()
            await release.wait()
        return result
    try:
        await h.simulator.action({"action": "handset", "state": "up"})
        await h.control("end")
        h.app._cleanup_session_resources = cleanup
        pending = asyncio.create_task(h.ready())
        await asyncio.wait_for(entered.wait(), 2)
        await h.simulator.action({"action": "handset", "state": "up"})
        assert h.state.handset == "up" and h.state.interaction_route is None
        release.set()
        assert (await pending)["reason"] == "profile_ready"
        assert h.state.mode == "profile_ready" and not h.state.interaction_open
        assert not h.app._active_audio
    finally:
        release.set()
        if pending:
            await asyncio.gather(pending, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
async def test_current_panel_route_survives_source_scan_clear_and_fresh_ready_at_up(tmp_path):
    h = PanelStand(tmp_path, identity=True)
    try:
        await h.control("start")
        await h.ready()
        await h.simulator.action({"action": "handset", "state": "up"})
        old = h.state.identity_request_id
        h.client.events.append({"kind": "clear", "reason_code": "scan_started"})
        assert (await h.boundary.step())["reason"] == "profile_cleared"
        assert h.state.interaction_route == "panel" and not h.app._canonical_profiles
        assert (await h.ready())["reason"] == "profile_ready"
        assert h.state.identity_request_id != old and h.state.mode == "voice_welcome"
        assert h.state.interaction_route == "panel" and h.state.handset == "up"
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", [False, True])
async def test_end_while_down_allows_next_physical_lift_for_new_visit(tmp_path, identity):
    h = PanelStand(tmp_path, identity=identity)
    try:
        if identity:
            await h.ready()
        await h.control("start")
        assert (await h.control("end"))["reason"] == "soft_end"
        assert h.state.mode == "session_closed" and h.state.handset == "down"
        assert not h.state.interaction_open
        lifted = await h.simulator.action({"action": "handset", "state": "up"})
        assert lifted["reason"] == "exhibition_handset_lifted"
        assert h.state.mode == "voice_welcome" and h.state.handset == "up"
        assert h.state.interaction_route == "handset" and h.state.interaction_open
        assert h.state.profile_status == "anonymous"
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_end_while_up_still_blocks_duplicate_lift_until_handset_placed(tmp_path):
    h = PanelStand(tmp_path, identity=True)
    try:
        await h.simulator.action({"action": "handset", "state": "up"})
        await h.ready()
        await h.control("start")
        assert (await h.control("end"))["reason"] == "soft_end"
        assert h.state.mode == "session_closed" and h.state.handset == "up"
        assert (await h.simulator.action({"action": "handset", "state": "up"}))["reason"] == "handset_already_lifted"
        assert not h.state.interaction_open
        assert (await h.simulator.action({"action": "handset", "state": "down"}))["reason"] == "closed_handset_placed"
        assert h.state.mode == "idle" and h.state.handset == "down"
        lifted = await h.simulator.action({"action": "handset", "state": "up"})
        assert lifted["reason"] == "exhibition_handset_lifted"
        assert h.state.mode == "voice_welcome" and h.state.handset == "up"
    finally:
        await h.close()


def test_canonical_runtime_end_while_down_allows_ready_then_lift():
    import time
    from app.foundation.canonical_contract import CanonicalEvent, CanonicalRuntime
    runtime = CanonicalRuntime(exhibition_mode=True, optional_identity=True)
    sid = "direct-end-down"
    now = time.monotonic()
    started = runtime.handle(CanonicalEvent("SessionStarted", sid, 1, 0, "panel", {}, "s1", now), session_action_authorized=True)
    assert started["reason"] == "panel_silent_session_started"
    ended = runtime.handle(CanonicalEvent("SessionCancel", sid, 2, started["state"]["state_version"], "panel", {}, "s2", now + 1), session_action_authorized=True)
    assert ended["reason"] == "soft_end" and ended["state"]["mode"] == "session_closed"
    ready = runtime.handle(CanonicalEvent("ClientProfileReady", sid, 3, ended["state"]["state_version"], "max", {
        "request_id": "req-1", "expires_monotonic": now + 100.0, "ready_deadline": now + 100.0,
    }, "s3", now + 2), profile={"profile_ref": "p1", "display_name": "Синтетик", "age_years": 6, "grammatical_gender": "male"}, identity_event_authorized=True)
    assert ready["reason"] == "profile_ready" and ready["state"]["mode"] == "profile_ready"
    lifted = runtime.handle(CanonicalEvent("HandsetLifted", sid, 4, ready["state"]["state_version"], "handset", {}, "s4", now + 3))
    assert lifted["reason"] == "exhibition_handset_lifted"
    assert lifted["state"]["mode"] == "voice_welcome" and lifted["state"]["profile_status"] == "identified"


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse_auth", [False, True])
async def test_physical_lift_entry_guest_and_reverse_auth_reach_menu_and_clean_next_visit(tmp_path, reverse_auth):
    h = PanelStand(tmp_path, identity=reverse_auth)
    try:
        lifted = await h.simulator.action({"action": "handset", "state": "up"})
        assert lifted["reason"] == "exhibition_handset_lifted"
        assert h.state.mode == "voice_welcome" and h.state.interaction_route == "handset"
        if reverse_auth:
            old = h.state
            assert (await h.ready())["reason"] == "profile_ready"
            assert h.state is not old and h.state.mode == "voice_welcome"
            assert h.state.profile_status == "identified" and h.state.handset == "up"
        await h.menu()
        await h.key(7)
        assert h.state.mode == "schedule_weather"
        await h.key(7)
        assert h.state.mode == "root_menu"
        assert (await h.control("end"))["reason"] == "soft_end"
        assert h.state.mode == "session_closed" and h.state.handset == "up"
        assert (await h.simulator.action({"action": "handset", "state": "down"}))["reason"] == "closed_handset_placed"
        next_lift = await h.simulator.action({"action": "handset", "state": "up"})
        assert next_lift["reason"] == "exhibition_handset_lifted"
        assert h.state.profile_status == "anonymous" and h.state.mode == "voice_welcome"
    finally:
        await h.close()
