from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.foundation.api import build_application
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.identity import IdentityDecision


class Clock:
    value = 0.0

    def __call__(self):
        return self.value


class GatedResolver:
    def __init__(self, clock, *, resist_cancel=False, capabilities=frozenset({"personalize", "schedule"})):
        self.clock, self.resist_cancel, self.capabilities = clock, resist_cancel, capabilities
        self.calls = []
        self.gates = {}

    async def resolve(self, uid, installation_id, request_id):
        self.calls.append((uid, installation_id, request_id))
        gate = self.gates.setdefault(uid, asyncio.Event())
        while not gate.is_set():
            try:
                await gate.wait()
            except asyncio.CancelledError:
                if not self.resist_cancel:
                    raise
        return IdentityDecision("authorized", {"profile_ref": "subject-" + uid[-1],
            "display_name": "PRIVATE_NAME", "schedule_ref": "PRIVATE_SCHEDULE",
            "date_of_birth": "2020-01-01", "sensory_flags": ["quiet"]},
            self.capabilities, 100.0)

    def readiness(self):
        return {"provider": "synthetic", "ready": True, "synthetic": True}


def make_app(resolver_factory=GatedResolver, **settings):
    clock = Clock()
    resolver = resolver_factory(clock)
    app, _, _, _ = build_application(settings=Settings(profile="mock", hardware_mode="fake",
        runtime_mode="exhibition", identity_mode="optional_rfid", identity_provider="synthetic",
        **settings), background_scheduler=False)
    app.monotonic_now = clock
    app.canonical_runtime.monotonic_now = clock
    app.identity_resolver = resolver
    return app, clock, resolver


async def send(app, kind, seq, payload=None, session="rfid"):
    return await app.receive_async(Event(kind, session, seq, 1000.0, "test", payload or {},
        f"{session}:{seq}", app.monotonic_now()))


async def present(app, uid, seq=1, session="rfid"):
    result = await send(app, "RfidPresented", seq, {"tag_id": uid}, session)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    return result


async def release(app, resolver, uid, session="rfid"):
    task = app._identity_lookup_tasks.get(session)
    resolver.gates[uid].set()
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), 1.0)


def test_lookup_is_nonblocking_and_lift_immediately_starts_guest_then_hangup():
    async def run():
        app, _, resolver = make_app(lambda clock: GatedResolver(clock, resist_cancel=True))
        result = await asyncio.wait_for(present(app, "CARD-A"), 0.5)
        assert result["reason"] == "identity_lookup_started"
        assert app.canonical_runtime.sessions["rfid"].mode == "rfid_lookup"
        lift = await asyncio.wait_for(send(app, "HandsetLifted", 2), 0.5)
        assert lift["canonical_state"]["mode"] == "voice_welcome"
        assert lift["canonical_state"]["profile_status"] == "anonymous"
        assert "rfid" not in app._identity_requests
        await asyncio.wait_for(send(app, "HandsetPlaced", 3), 0.5)
        resolver.gates["CARD-A"].set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert "rfid" not in app._canonical_profiles
        assert "rfid" not in app.canonical_runtime.sessions
        await app._cleanup_session_resources("rfid")
    asyncio.run(run())


def test_replacement_card_fences_late_authorized_result():
    async def run():
        app, _, resolver = make_app(lambda clock: GatedResolver(clock, resist_cancel=True))
        await present(app, "CARD-A")
        await present(app, "CARD-B", 2)
        await release(app, resolver, "CARD-B")
        assert app._canonical_profiles["rfid"]["profile_ref"] == "subject-B"
        resolver.gates["CARD-A"].set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert app._canonical_profiles["rfid"]["profile_ref"] == "subject-B"
        assert app.canonical_runtime.sessions["rfid"].mode == "rfid_ready"
        await app._cleanup_session_resources("rfid")
    asyncio.run(run())


def test_duplicate_pending_and_ready_cards_do_not_renew_or_restart():
    async def run():
        app, clock, resolver = make_app(rfid_ready_ttl_s=5.0)
        await present(app, "CARD-A")
        first = app._identity_requests["rfid"]
        clock.value = 0.3
        duplicate = await present(app, "CARD-A", 2)
        assert duplicate["reason"] == "rfid_repeated"
        assert app._identity_requests["rfid"] is first
        assert len(resolver.calls) == 1
        await release(app, resolver, "CARD-A")
        state = app.canonical_runtime.sessions["rfid"]
        deadline = state.identity_ready_deadline
        version = state.state_version
        clock.value = 2.0
        duplicate = await present(app, "CARD-A", 3)
        assert duplicate["outputs"] == []
        assert state.identity_ready_deadline == deadline and state.state_version == version
        assert len(resolver.calls) == 1
        await app._cleanup_session_resources("rfid")
    asyncio.run(run())


def test_timeout_and_late_allow_fail_closed():
    async def run():
        app, clock, resolver = make_app(lambda clock: GatedResolver(clock, resist_cancel=True))
        await present(app, "CARD-A")
        clock.value = 2.0
        results = await app.expire_identity_leases()
        assert results[0]["reason"] == "identity_lookup_failed"
        resolver.gates["CARD-A"].set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert app.canonical_runtime.sessions["rfid"].mode == "idle"
        assert not app._canonical_profiles and not app._identity_requests
        await app._cleanup_session_resources("rfid")
    asyncio.run(run())


def test_ready_lift_preserves_profile_and_in_call_scan_skips_resolver():
    async def run():
        app, _, resolver = make_app()
        await present(app, "CARD-A")
        await release(app, resolver, "CARD-A")
        result = await send(app, "HandsetLifted", 2)
        assert result["canonical_state"]["mode"] == "voice_welcome"
        assert result["canonical_state"]["profile_status"] == "identified"
        assert any(effect.get("preserve_current") is True for effect in result["outputs"])
        ignored = await present(app, "CARD-B", 3)
        assert ignored["reason"] == "rfid_active_session_ignored"
        assert len(resolver.calls) == 1
        await send(app, "HandsetPlaced", 4)
        assert not app._canonical_profiles and not app._identity_requests
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["IdentityLookupSucceeded", "IdentityLookupFailed", "IdentityReadyExpired", "IdentityAuthorizationExpired"])
def test_generic_ingress_cannot_forge_identity_authority(kind):
    async def run():
        app, _, resolver = make_app()
        await present(app, "CARD-A")
        request = app._identity_requests["rfid"]["request_id"]
        result = await send(app, kind, 2, {"request_id": request, "authorized": True,
            "identity_event_authorized": True, "capabilities": ["personalize"],
            "profile": {"profile_ref": "forged"}, "expires_monotonic": 90.0})
        assert result["reason"] == "identity_outcome_untrusted"
        assert app.canonical_runtime.sessions["rfid"].mode == "rfid_lookup"
        await app._cleanup_session_resources("rfid")
        resolver.gates["CARD-A"].set()
    asyncio.run(run())


@pytest.mark.parametrize("ending", ["AnonymousStart", "RfidRemoved", "SessionCancel", "SessionReset"])
def test_identity_end_clears_profile_context_audio_ownership_and_public_uid(ending):
    async def run():
        app, _, resolver = make_app()
        uid = "PRIVATE_UID_A"
        await present(app, uid)
        assert uid not in json.dumps(app.last_canonical_event)
        assert uid not in json.dumps(list(app.publisher.events))
        await release(app, resolver, uid)
        context = app._canonical_audio_context("rfid")
        assert context.profile.display_name == "PRIVATE_NAME"
        app.conversation_contexts["rfid"] = context
        await send(app, ending, 2)
        assert "rfid" not in app._canonical_profiles
        assert "rfid" not in app._canonical_schedule_refs
        assert "rfid" not in app.conversation_contexts
        assert "rfid" not in app._identity_requests
        assert "rfid" not in app._active_audio
        assert app.sessions.get("rfid").rfid_tag is None
        assert "PRIVATE_NAME" not in app._canonical_audio_context("rfid").system_prompt
        await app._cleanup_session_resources("rfid")
    asyncio.run(run())


def test_ready_and_active_expiry_and_independent_guest():
    async def run():
        app, clock, resolver = make_app(rfid_ready_ttl_s=3.0)
        await present(app, "CARD-A")
        await release(app, resolver, "CARD-A")
        clock.value = 3.0
        assert (await app.expire_identity_leases())[0]["reason"] == "identity_expired"
        assert app.canonical_runtime.sessions["rfid"].mode == "idle"
        await present(app, "CARD-B", 2)
        await release(app, resolver, "CARD-B")
        await send(app, "HandsetLifted", 3)
        blocked = await send(app, "HandsetLifted", 1, session="guest")
        assert blocked["reason"] == "installation_busy"
        clock.value = 100.0
        await app.expire_identity_leases()
        assert app.canonical_runtime.sessions["rfid"].mode == "session_closed"
        assert "guest" not in app.canonical_runtime.sessions
        assert not app._canonical_profiles
        await send(app, "HandsetPlaced", 4)
        assert app.canonical_runtime.sessions["rfid"].mode == "idle"
        await send(app, "HandsetLifted", 2, session="guest")
        assert app.canonical_runtime.sessions["guest"].profile_status == "anonymous"
        assert app.canonical_runtime.sessions["guest"].mode == "voice_welcome"
        await app._cleanup_session_resources("rfid")
        await app._cleanup_session_resources("guest")
    asyncio.run(run())


@pytest.mark.parametrize("capabilities", [frozenset(), frozenset({"schedule"})])
def test_personal_context_requires_personalize_capability(capabilities):
    async def run():
        app, _, resolver = make_app(lambda clock: GatedResolver(clock, capabilities=capabilities))
        await present(app, "CARD-A")
        await release(app, resolver, "CARD-A")
        assert app._canonical_audio_context("rfid").profile.display_name is None
        assert "PRIVATE_NAME" not in app._canonical_audio_context("rfid").system_prompt
        assert bool(app._canonical_schedule_refs["rfid"]) == ("schedule" in capabilities)
        status = app.identity_status()
        assert status["identity_ready"] and status["synthetic"] and not status["reader_ready"]
        await app._cleanup_session_resources("rfid")
    asyncio.run(run())


def test_failed_lookup_debounce_is_bounded_and_different_card_can_replace():
    class UnknownResolver(GatedResolver):
        async def resolve(self, uid, installation_id, request_id):
            self.calls.append((uid, installation_id, request_id))
            return IdentityDecision("unknown")

    async def run():
        app, clock, resolver = make_app(UnknownResolver, rfid_debounce_s=0.5)
        await present(app, "CARD-A")
        task = app._identity_lookup_tasks.get("rfid")
        if task is not None:
            await task
        assert app.canonical_runtime.sessions["rfid"].mode == "idle"
        clock.value = 0.1
        assert (await present(app, "CARD-A", 2))["reason"] == "rfid_debounced"
        assert len(resolver.calls) == 1
        clock.value = 0.5
        assert (await present(app, "CARD-A", 3))["reason"] == "identity_lookup_started"
        task = app._identity_lookup_tasks.get("rfid")
        if task is not None:
            await task
        assert len(resolver.calls) == 2
        clock.value = 0.6
        assert (await present(app, "CARD-B", 4))["reason"] == "identity_lookup_started"
        await app.shutdown_identity()
    asyncio.run(run())


def test_second_session_cannot_steal_lookup_or_ready_installation():
    async def run():
        app, _, resolver = make_app()
        await present(app, "CARD-A")
        request = app._identity_requests["rfid"]
        assert (await present(app, "CARD-B", session="other"))["reason"] == "installation_busy"
        assert app._identity_requests["rfid"] is request
        assert len(resolver.calls) == 1
        await release(app, resolver, "CARD-A")
        state = app.canonical_runtime.sessions["rfid"].snapshot()
        assert (await send(app, "AnonymousStart", 2, session="other"))["reason"] == "installation_busy"
        assert app.canonical_runtime.sessions["rfid"].snapshot() == state
        assert app._canonical_profiles["rfid"]["profile_ref"] == "subject-A"
        assert "other" not in app.canonical_runtime.sessions
        await app.shutdown_identity()
    asyncio.run(run())


def test_shutdown_is_bounded_and_revokes_cancellation_resistant_lookup():
    async def run():
        app, _, resolver = make_app(lambda clock: GatedResolver(clock, resist_cancel=True), audio_cleanup_timeout_s=0.01)
        await present(app, "CARD-A")
        result = await asyncio.wait_for(app.shutdown_identity(), 0.5)
        assert result["remaining"] <= 1
        assert not app._identity_requests and not app._identity_lookup_tasks
        resolver.gates["CARD-A"].set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not app._canonical_profiles
        assert (await present(app, "CARD-B", 2))["reason"] == "identity_shutdown"
    asyncio.run(run())


@pytest.mark.parametrize("readiness", [None, [], "bad"])
def test_malformed_readiness_does_not_break_guest_start(readiness):
    async def run():
        app, _, resolver = make_app()
        resolver.readiness = lambda: readiness
        assert app.identity_status()["identity_ready"] is False
        assert (await send(app, "HandsetLifted", 1))["canonical_state"]["profile_status"] == "anonymous"
        await app._cleanup_session_resources("rfid")
    asyncio.run(run())


def test_lease_expiring_while_private_schedule_is_awaited_discards_result():
    class Schedule:
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.calls = []

        async def schedule(self, profile, reference):
            self.calls.append((profile, reference))
            self.started.set()
            await self.release.wait()
            return [{"time": "12:00", "title": "PRIVATE_TOOL_RESULT"}]

    async def run():
        app, clock, resolver = make_app()
        await present(app, "CARD-A")
        await release(app, resolver, "CARD-A")
        await send(app, "HandsetLifted", 2)
        state = app.canonical_runtime.sessions["rfid"]
        state.mode = "schedule_weather"
        schedule = Schedule()
        app.schedule_weather = schedule
        pending = asyncio.create_task(send(app, "ButtonPressed", 3, {"button_id": 3}))
        await asyncio.wait_for(schedule.started.wait(), 0.5)
        clock.value = 100.0
        schedule.release.set()
        result = await asyncio.wait_for(pending, 0.5)
        assert result["reason"] == "stale_session_generation"
        assert "PRIVATE_TOOL_RESULT" not in json.dumps(result)
        assert "PRIVATE_TOOL_RESULT" not in json.dumps(app.last_canonical_effects)
        assert app.canonical_runtime.sessions["rfid"].mode == "session_closed"
        assert not app._canonical_profiles
        await app.shutdown_identity()
    asyncio.run(run())


def test_schedule_tool_requires_schedule_capability_before_provider_call():
    class Schedule:
        async def schedule(self, *_args):
            raise AssertionError("schedule capability missing")

    async def run():
        app, _, resolver = make_app(lambda clock: GatedResolver(clock, capabilities=frozenset({"personalize"})))
        await present(app, "CARD-A")
        await release(app, resolver, "CARD-A")
        await send(app, "HandsetLifted", 2)
        app.canonical_runtime.sessions["rfid"].mode = "schedule_weather"
        app.schedule_weather = Schedule()
        result = await send(app, "ButtonPressed", 3, {"button_id": 3})
        assert result["outputs"][0]["resolution"] == "typed_information_blocked_by_identity"
        await app.shutdown_identity()
    asyncio.run(run())


def test_cancellation_resistant_provider_work_remains_bounded():
    async def run():
        app, _, resolver = make_app(lambda clock: GatedResolver(clock, resist_cancel=True), audio_cleanup_timeout_s=0.01)
        try:
            for index in range(12):
                await present(app, "CARD-" + str(index), index + 1)
                await asyncio.sleep(0)
                assert len(app._identity_provider_tasks) <= app._identity_worker_limit
                assert len(app._identity_lookup_tasks) <= 1
            assert len(resolver.calls) == app._identity_worker_limit
        finally:
            for gate in resolver.gates.values():
                gate.set()
            await app.shutdown_identity()
        assert not app._identity_requests
    asyncio.run(run())


@pytest.mark.parametrize("decision", [
    {"status": "authorized", "profile": {"profile_ref": "forged"}},
    IdentityDecision("authorized", {"profile_ref": "subject"}, frozenset({"admin"}), 100.0),
    IdentityDecision("authorized", {"profile_ref": "subject"}, frozenset({"personalize"}), float("nan")),
])
def test_every_injected_resolver_decision_is_validated_again(decision):
    class BrokenResolver(GatedResolver):
        async def resolve(self, uid, installation_id, request_id):
            return decision

    async def run():
        app, _, _ = make_app(BrokenResolver)
        await present(app, "CARD-A")
        task = app._identity_lookup_tasks.get("rfid")
        if task is not None:
            await task
        assert app.canonical_runtime.sessions["rfid"].mode == "idle"
        assert not app._canonical_profiles
        await app.shutdown_identity()
    asyncio.run(run())


def test_audio_completion_expiry_does_not_cancel_or_await_itself():
    async def run():
        app, clock, resolver = make_app()
        await present(app, "CARD-A")
        await release(app, resolver, "CARD-A")
        await send(app, "HandsetLifted", 2)
        record = app.sessions.get("rfid")
        epoch = app._session_epochs.get("rfid", 0)
        pending = asyncio.get_running_loop().create_future()
        completion = asyncio.create_task(app._complete_canonical_audio_turn(
            "rfid", 3, pending, epoch, record.correlation_id))
        app._audio_completions["rfid"] = {completion}
        clock.value = 100.0
        pending.set_result(SimpleNamespace(stt=SimpleNamespace(text="PRIVATE_TRANSCRIPT")))
        await asyncio.wait_for(completion, 0.5)
        assert not completion.cancelled()
        assert app.canonical_runtime.sessions["rfid"].mode == "session_closed"
        assert not app._canonical_profiles
        assert "PRIVATE_TRANSCRIPT" not in json.dumps(app.last_canonical_event)
        app._audio_completions.pop("rfid", None)
        await app.shutdown_identity()
    asyncio.run(run())


@pytest.mark.parametrize("ending", ["HandsetPlaced", "SessionCancel", "SessionReset", "AnonymousStart", "application_reset"])
def test_urgent_identity_cleanup_preserves_only_incident(ending):
    async def run():
        app, _, resolver = make_app()
        await present(app, "CARD-A")
        await release(app, resolver, "CARD-A")
        await send(app, "HandsetLifted", 2)
        state = app.canonical_runtime.sessions["rfid"]
        state.mode, state.safety_severity, state.incident_id = "safety_handoff", "urgent", "synthetic-incident"
        state.dialogue_memory.bounded_turns.append("PRIVATE_TURN")
        app.conversation_contexts["rfid"] = app._canonical_audio_context("rfid")
        if ending == "application_reset":
            await app.reset_session("rfid")
        else:
            await send(app, ending, 3)
        assert state.mode == "safety_handoff" and state.safety_severity == "urgent"
        assert state.incident_id == "synthetic-incident"
        assert state.profile_ref is None and state.display_name is None and state.age_years is None
        assert not state.dialogue_memory.bounded_turns
        assert not app._canonical_profiles and not app.conversation_contexts
        assert not app._identity_requests
        await app.shutdown_identity()
    asyncio.run(run())


def test_stale_replacement_cleanup_cannot_erase_new_card_resources():
    async def run():
        app, _, resolver = make_app()
        await present(app, "CARD-A")
        await release(app, resolver, "CARD-A")
        entered, proceed = asyncio.Event(), asyncio.Event()
        original = app._cancel_audio_completions
        calls = 0

        async def delayed_cleanup(session_id, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await proceed.wait()
            return await original(session_id, **kwargs)

        app._cancel_audio_completions = delayed_cleanup
        old = asyncio.create_task(present(app, "CARD-B", 2))
        try:
            await asyncio.wait_for(entered.wait(), 0.5)
            await present(app, "CARD-C", 3)
            await release(app, resolver, "CARD-C")
            context = app._canonical_audio_context("rfid")
            app.conversation_contexts["rfid"] = context
            lease = {"content_id": "card-C-owned", "generation": 3}
            app._active_audio["rfid"] = lease
            app._last_audio_ack_at["rfid"] = 7.0
            proceed.set()
            assert (await asyncio.wait_for(old, 0.5))["reason"] == "stale_session_generation"
            assert app._canonical_profiles["rfid"]["profile_ref"] == "subject-C"
            assert app._canonical_schedule_refs["rfid"] == "PRIVATE_SCHEDULE"
            assert app.conversation_contexts["rfid"] is context
            assert app._active_audio["rfid"] is lease
            assert app._last_audio_ack_at["rfid"] == 7.0
            assert app.canonical_runtime.sessions["rfid"].mode == "rfid_ready"
        finally:
            proceed.set()
            await asyncio.gather(old, return_exceptions=True)
            await app.shutdown_identity()
    asyncio.run(run())


def test_duplicate_during_replacement_cleanup_cannot_renew_request():
    async def run():
        app, clock, resolver = make_app()
        await present(app, "CARD-A")
        await release(app, resolver, "CARD-A")
        entered, proceed = asyncio.Event(), asyncio.Event()
        original = app._cancel_audio_completions

        async def delayed_cleanup(session_id, **kwargs):
            entered.set()
            await proceed.wait()
            return await original(session_id, **kwargs)

        app._cancel_audio_completions = delayed_cleanup
        replacement = asyncio.create_task(present(app, "CARD-B", 2))
        try:
            await asyncio.wait_for(entered.wait(), 0.5)
            owned = app._identity_requests["rfid"]
            clock.value = 0.2
            repeated = await present(app, "CARD-B", 3)
            assert repeated["reason"] == "rfid_repeated"
            assert app._identity_requests["rfid"] is owned
            assert owned["lookup_deadline"] == 2.0
            proceed.set()
            await asyncio.wait_for(replacement, 0.5)
            await release(app, resolver, "CARD-B")
            assert len(resolver.calls) == 2
        finally:
            proceed.set()
            await asyncio.gather(replacement, return_exceptions=True)
            await app.shutdown_identity()
    asyncio.run(run())


def test_failed_scan_debounce_uses_acceptance_time_before_delayed_hook():
    class UnknownResolver(GatedResolver):
        async def resolve(self, *_args):
            self.calls.append(True)
            return IdentityDecision("unknown")

    async def run():
        app, clock, resolver = make_app(UnknownResolver, rfid_debounce_s=0.5)
        entered, proceed = asyncio.Event(), asyncio.Event()

        async def hook(_session_id, result):
            if result["reason"] == "identity_lookup_started":
                entered.set()
                await proceed.wait()

        app.state_update_hook = hook
        first = asyncio.create_task(present(app, "CARD-A"))
        try:
            await asyncio.wait_for(entered.wait(), 0.5)
            clock.value = 0.4
            assert (await present(app, "CARD-A", 2))["reason"] == "rfid_repeated"
            proceed.set()
            await asyncio.wait_for(first, 0.5)
            task = app._identity_lookup_tasks.get("rfid")
            if task is not None:
                await task
            clock.value = 0.5
            repeated = await present(app, "CARD-A", 3)
            assert repeated["reason"] == "identity_lookup_started"
            task = app._identity_lookup_tasks.get("rfid")
            if task is not None:
                await task
            assert len(resolver.calls) == 2
        finally:
            proceed.set()
            await asyncio.gather(first, return_exceptions=True)
            await app.shutdown_identity()
    asyncio.run(run())
