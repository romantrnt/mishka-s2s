"""Real unchanged Max client + HTTPS + SQLite → real MISHKA mock application.

Only capture and physical hardware are simulated. No manufactured ready profile
is substituted on the E2E path. No speech model or actual audio device is used.
"""
from __future__ import annotations
import asyncio
import importlib.util
import os
import json
import queue
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
# Only these external-client E2E tests require the separately supplied Max package.
# A configured but broken package must fail collection, not silently skip.
MAX_BUNDLE = os.environ.get("MISHKA_TEST_MAX_BUNDLE")
if MAX_BUNDLE:
    bundle = Path(MAX_BUNDLE).expanduser()
    if not bundle.is_absolute() or not bundle.exists():
        raise RuntimeError("MISHKA_TEST_MAX_BUNDLE must name an existing absolute ZIP or source directory")
    sys.path.insert(0, str(bundle))
if importlib.util.find_spec("phone_local_demo") is None:
    if MAX_BUNDLE:
        raise RuntimeError("MISHKA_TEST_MAX_BUNDLE does not contain phone_local_demo")
    raise unittest.SkipTest(
        "External Max client E2E requires MISHKA_TEST_MAX_BUNDLE=/absolute/client.zip "
        "or its source directory on PYTHONPATH; core JSON contract tests remain enabled"
    )
from phone_local_demo import initialize, DemoServer, connect_demo
from phone_demo_database import demo_frame

from app.foundation.api import build_application
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.events import parse_event, EventValidationError
from app.foundation.max_json import MaxJsonBoundary


class Capture:
    def __init__(self):
        self.commands = queue.Queue()
    def __call__(self, handler, devices, stop):
        self.devices = devices
        devices("connected")
        while not stop.is_set():
            try:
                code, done = self.commands.get(timeout=.02)
            except queue.Empty:
                continue
            handler(demo_frame(code))
            done.set()
        return 0
    async def scan(self, code):
        done = threading.Event()
        self.commands.put((code, done))
        if not await asyncio.to_thread(done.wait, 12):
            raise AssertionError("capture_timeout")


class RealJsonIntegration(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="mishka-json-e2e-")
        cls.directory = initialize(Path(cls.temp.name) / "synthetic")
        cls.server = DemoServer(cls.directory, port=0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join(5)
        cls.server.server_close()
        cls.temp.cleanup()
    async def asyncSetUp(self):
        self.capture = Capture()
        self.client = connect_demo(self.directory, port=self.port, capture_runner=self.capture)
        self.app, *_ = build_application(settings=Settings(profile="mock", hardware_mode="fake",
            runtime_mode="exhibition", identity_mode="max_json"),
            background_scheduler=False)
        self.boundary = MaxJsonBoundary(self.app, self.client)
        self.boundary.start(background=False)
        self.sequence = 0
        await self.drain()
    async def asyncTearDown(self):
        await self.boundary.close()
        await self.app.shutdown_identity()
    async def drain(self):
        out = []
        for _ in range(30):
            result = await self.boundary.step()
            if result is None:
                break
            out.append(result)
        return out
    async def scan(self, code):
        await self.capture.scan(code)
        return await self.drain()
    async def send(self, kind, payload=None):
        self.sequence += 1
        return await self.app.receive_async(Event(kind, self.boundary.session_id, self.sequence,
            time.time(), "test_handset", payload or {}, f"handset-{self.sequence}", time.monotonic()))
    def state(self):
        return self.app.canonical_runtime.sessions.get(self.boundary.session_id)

    async def test_real_json_reaches_canonical_and_prompt_without_uid_resolver(self):
        class ForbiddenResolver:
            async def resolve(self, *_):
                raise AssertionError("MISHKA must not resolve UID")
        self.assertFalse(hasattr(self.app, "identity_resolver"))
        results = await self.scan("demo-1")
        self.assertIn("profile_ready", [x["reason"] for x in results])
        state = self.state()
        self.assertEqual(state.mode, "profile_ready")
        self.assertEqual(state.display_name, "Демо")
        self.assertEqual(state.age_band_source, "data14_age_years")
        self.assertIsInstance(state.age_years, int)
        self.assertTrue(state.profile_ref.startswith("max-context:"))
        self.assertFalse(hasattr(self.app, "_identity_lookup_tasks"))
        self.assertFalse(hasattr(self.app, "_identity_provider_tasks"))
        context = self.app._canonical_audio_context(self.boundary.session_id)
        self.assertEqual(context.profile.display_name, "Демо")
        self.assertEqual(context.profile.age, state.age_years)
        self.assertIn("Демо", context.system_prompt)
        self.assertNotIn("Вымышленный тестовый диагноз", context.system_prompt)
        self.assertNotIn("Тестовый специалист", context.system_prompt)
        self.assertNotIn("patient_profile", self.app.last_canonical_event["payload"])

    async def test_real_max_json_starts_menu_without_exhibition_identity_questions(self):
        self.app.audio.private_pcm_supported = False  # Recorded fallback path.
        await self.scan("demo-1")
        await self.send("HandsetLifted")
        deadline = self.state().welcome_deadline_monotonic
        self.app.monotonic_now = lambda: deadline + .01
        async def accepted(kind, seq, payload=None):
            return await self.app.receive_async(Event(kind, self.boundary.session_id, seq,
                time.time(), "profile_onboarding", payload or {}, f"profile-onboarding-{seq}", deadline + .01))
        await accepted("WelcomeTimerFired", 1)
        lease = self.app._active_audio[self.boundary.session_id]
        callback = {key: lease[key] for key in ("token", "generation", "playback_lease", "playback_id")}
        callback["response_id"] = "PHRASE_001"
        result = await accepted("AudioCompleted", 2, callback)
        self.assertEqual(result["reason"], "authorized_profile_menu_started")
        self.assertEqual(self.state().display_name, "Демо")
        self.assertEqual(self.state().age_band_source, "data14_age_years")
        self.assertEqual([x.get("phrase_id") for x in result["outputs"] if x.get("type") == "speak"], ["PHRASE_007"])
        duplicate = await accepted("AudioCompleted", 3, callback)
        self.assertNotEqual(duplicate["reason"], "exhibition_introduction_started")
        self.assertEqual(self.state().mode, "exhibition_menu_tutorial_prompt")
        self.assertNotIn("Демо", json.dumps(self.state().snapshot(), ensure_ascii=False))

    async def test_real_max_json_private_welcome_waits_for_child_answer(self):
        await self.scan("demo-1")
        await self.send("HandsetLifted")
        owner = self.app._private_welcomes[self.boundary.session_id]
        await owner.task
        deadline = self.state().welcome_deadline_monotonic
        self.app.monotonic_now = lambda: deadline
        async def accepted(kind, seq, payload=None, text=None):
            return await self.app.receive_async(Event(kind, self.boundary.session_id, seq,
                time.time(), "private_onboarding", payload or {}, f"private-onboarding-{seq}", deadline), text)
        result = await accepted("WelcomeTimerFired", 1)
        self.assertEqual(result["reason"], "personal_welcome_started")
        lease = self.app._active_audio[self.boundary.session_id]
        callback = {key: lease[key] for key in ("token", "generation", "playback_lease", "playback_id")}
        callback["response_id"] = "PERSONAL_WELCOME"
        result = await accepted("AudioCompleted", 2, callback)
        self.assertEqual(result["reason"], "personal_welcome_listening")
        self.assertFalse(any(x.get("phrase_id") == "PHRASE_007" for x in result["outputs"]))
        result = await accepted("SpeechReceived", 3, text="Хорошо")
        self.assertEqual(result["reason"], "personal_welcome_answer_received")
        self.assertEqual(self.state().mode, "exhibition_menu_tutorial_prompt")
        self.assertFalse(self.app.bounded_s2s.tts.cache)
        self.assertNotIn("Демо", json.dumps(result, ensure_ascii=False))

    async def test_ready_lift_hangup_clears_both_sides(self):
        await self.scan("demo-1")
        await self.send("HandsetLifted")
        self.assertEqual(self.state().handset, "up")
        self.assertEqual(self.state().profile_status, "identified")
        await self.send("HandsetPlaced")
        self.assertFalse(self.app._canonical_profiles)
        self.assertIsNone(self.client.patient())
        self.assertIsNone(self.boundary.facts)

    async def test_unknown_and_archived_clear_existing_context(self):
        for code in ("unknown", "demo-3"):
            await self.scan("demo-1")
            await self.scan(code)
            self.assertFalse(self.app._canonical_profiles)
            self.assertIsNone(self.client.patient())
            self.assertIsNone(self.state().profile_ref)

    async def test_client_120_second_expiry_clears_mishka(self):
        await self.scan("demo-1")
        self.client._clock = lambda: time.monotonic() + 121
        await self.drain()
        self.assertIsNone(self.state().profile_ref)
        self.assertFalse(self.app._canonical_profiles)

    async def test_queued_ready_cannot_survive_guest_lift(self):
        await self.capture.scan("demo-1")
        await self.send("HandsetLifted")
        await self.drain()
        self.assertEqual(self.state().profile_status, "anonymous")
        self.assertFalse(self.app._canonical_profiles)
        self.assertIsNone(self.client.patient())

    async def test_existing_duplicate_bug_is_exposed_not_hidden(self):
        await self.scan("demo-1")
        await self.scan("demo-1")
        # Known upstream defect, preserved and documented, not compensated for.
        self.assertIsNone(self.client.patient())
        self.assertIsNone(self.state().profile_ref)

    async def test_scan_during_call_exposes_conservative_policy_change(self):
        await self.scan("demo-1")
        await self.send("HandsetLifted")
        await self.scan("demo-2")
        self.assertEqual(self.state().mode, "session_closed")
        self.assertIsNone(self.state().profile_ref)
        self.assertFalse(self.app._canonical_profiles)

    async def test_urgent_incident_survives_new_scan_while_private_context_is_cleared(self):
        await self.scan("demo-1")
        await self.send("HandsetLifted")
        await self.send("VoiceIntentReady", {"intent": "free_speech", "safety_severity": "urgent"})
        incident = self.state().incident_id
        self.assertIsNotNone(incident)
        await self.scan("demo-2")
        self.assertEqual(self.state().mode, "safety_handoff")
        self.assertEqual(self.state().incident_id, incident)
        self.assertEqual(self.state().safety_severity, "urgent")
        self.assertIsNone(self.state().profile_ref)
        self.assertFalse(self.app._canonical_profiles)
        self.assertIsNone(self.boundary.facts)
        self.assertIsNone(self.client.patient())

    async def _interleave_ready_hook(self, kind):
        entered, release = asyncio.Event(), asyncio.Event()
        async def hook(_session_id, update):
            if update.get("reason") == "profile_ready":
                entered.set()
                await release.wait()
        self.app.state_update_hook = hook
        await self.capture.scan("demo-1")
        pending = asyncio.create_task(self.drain())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            request = self.state().identity_request_id
            result = await self.send(kind)
            release.set()
            ready_results = await asyncio.wait_for(pending, 2)
            return result, ready_results, request
        finally:
            release.set()
            await pending
            self.app.state_update_hook = None

    async def test_duplicate_down_while_ready_hook_waits_preserves_one_live_owner(self):
        result, ready_results, request = await self._interleave_ready_hook("HandsetPlaced")
        self.assertEqual(result["reason"], "handset_already_placed")
        self.assertIn("profile_ready", [item["reason"] for item in ready_results])
        self.assertEqual(self.state().identity_request_id, request)
        self.assertEqual(self.state().mode, "profile_ready")
        self.assertTrue(self.app._identity_scope_live(self.boundary.session_id))
        self.assertTrue(self.app._canonical_profiles)
        self.assertIsNotNone(self.boundary.facts)
        self.assertIsNotNone(self.client.patient())

    async def test_lift_while_ready_hook_waits_keeps_same_profile_owner(self):
        result, ready_results, request = await self._interleave_ready_hook("HandsetLifted")
        self.assertEqual(result["reason"], "exhibition_handset_lifted")
        self.assertEqual(self.state().identity_request_id, request)
        self.assertEqual(self.state().handset, "up")
        self.assertTrue(self.app._identity_scope_live(self.boundary.session_id))
        self.assertTrue(self.app._canonical_profiles)
        self.assertIsNotNone(self.boundary.facts)
        self.assertIsNotNone(self.client.patient())
        await self.send("HandsetPlaced")
        self.assertFalse(self.app._canonical_profiles)
        self.assertIsNone(self.boundary.facts)
        self.assertIsNone(self.client.patient())

    async def test_cancel_while_ready_hook_waits_clears_both_without_resurrection(self):
        result, ready_results, request = await self._interleave_ready_hook("SessionCancel")
        self.assertIsNone(self.state().profile_ref)
        self.assertFalse(self.app._identity_scope_live(self.boundary.session_id))
        self.assertFalse(self.app._canonical_profiles)
        self.assertIsNone(self.boundary.facts)
        self.assertIsNone(self.client.patient())

    async def test_ready_json_does_not_grant_schedule_or_query_a_schedule_provider(self):
        calls = []
        class ForbiddenSchedule:
            async def schedule(self, *_args):
                calls.append("schedule")
                raise AssertionError("DATA-14 doctors_today is not a schedule grant")
        self.app.audio.private_pcm_supported = False  # Existing recorded welcome path.
        await self.scan("demo-1")
        await self.send("HandsetLifted")
        deadline = self.state().welcome_deadline_monotonic
        self.app.monotonic_now = lambda: deadline + .01
        async def accepted(kind, payload=None):
            self.sequence += 1
            return await self.app.receive_async(Event(kind, self.boundary.session_id, self.sequence,
                time.time(), "test_handset", payload or {}, f"handset-{self.sequence}", deadline + .01))
        await accepted("WelcomeTimerFired")
        lease = self.app._active_audio[self.boundary.session_id]
        await accepted("AudioCompleted", {"response_id": "PHRASE_001",
            **{key: lease[key] for key in ("token", "generation", "playback_lease", "playback_id")}})
        await accepted("ButtonPressed", {"button_id": 1})
        await accepted("ButtonReleased", {"button_id": 1})
        self.assertEqual(self.state().mode, "menu_ready")
        self.app.schedule_weather = ForbiddenSchedule()
        await accepted("ButtonPressed", {"button_id": 7})
        await accepted("ButtonReleased", {"button_id": 7})
        self.assertEqual(self.state().mode, "schedule_weather")
        self.assertFalse(any(item["id"] in {"what_next", "where_now"}
            for item in self.state().scene_menu_descriptor["items"]))
        result = await accepted("ButtonPressed", {"button_id": 3})
        await accepted("ButtonReleased", {"button_id": 3})
        self.assertFalse(any(x.get("type") == "typed_info_request" for x in result["outputs"]))
        self.sequence += 1
        result = await self.app.receive_async(Event("SpeechReceived", self.boundary.session_id,
            self.sequence, time.time(), "test_handset", {}, f"handset-{self.sequence}", deadline + .01),
            "Куда мне сейчас идти?")
        self.assertTrue(any(x.get("resolution") == "typed_information_blocked_by_identity" for x in result["outputs"]))
        self.assertFalse(calls)
        self.assertFalse(self.app._identity_scope_live(self.boundary.session_id, "schedule"))
        self.assertIsNone(self.app._canonical_schedule_refs[self.boundary.session_id])

    async def test_another_session_cannot_take_a_ready_profile(self):
        await self.scan("demo-1")
        reference = self.state().profile_ref
        result = await self.app.receive_async(Event("HandsetLifted", "different-session", 1,
            time.time(), "other_handset", {}, "other-1", time.monotonic()))
        self.assertEqual(result["reason"], "installation_busy")
        self.assertEqual(self.state().profile_ref, reference)
        self.assertIsNotNone(self.client.patient())
        self.assertNotIn("different-session", self.app._canonical_profiles)

    async def test_ready_without_dropped_clear_cannot_keep_previous_active_profile(self):
        await self.scan("demo-1")
        await self.send("HandsetLifted")
        await self.capture.scan("demo-2")
        for _ in range(7):
            self.capture.devices("connected")
        results = await self.drain()
        self.assertIn("profile_active_session_ignored", [item["reason"] for item in results])
        self.assertIsNone(self.state().profile_ref)
        self.assertFalse(self.app._canonical_profiles)
        self.assertIsNone(self.boundary.facts)
        self.assertIsNone(self.client.patient())
        self.assertFalse(self.app._identity_scope_live(self.boundary.session_id))

    async def test_ready_without_dropped_clear_has_a_fresh_context_generation(self):
        await self.scan("demo-1")
        reference = self.state().profile_ref
        await self.capture.scan("demo-2")
        for _ in range(7):
            self.capture.devices("connected")
        await self.drain()
        self.assertNotEqual(self.state().profile_ref, reference)
        self.assertTrue(self.app._identity_scope_live(self.boundary.session_id))
        self.assertIsNotNone(self.client.patient())
        self.assertTrue(self.boundary.facts["wheelchair_access"])

    async def test_actual_reset_drops_ready_and_queued_card(self):
        for queued in (False, True):
            with self.subTest(queued=queued):
                await self.capture.scan("demo-1" if queued else "demo-2")
                if not queued:
                    await self.drain()
                result = await self.app.reset_session(self.boundary.session_id)
                await self.drain()
                self.assertTrue(result["reset"])
                self.assertFalse(self.app._canonical_profiles)
                self.assertIsNone(self.boundary.facts)
                self.assertIsNone(self.client.patient())
                self.assertNotIn(self.boundary.session_id, self.app.canonical_runtime.sessions)

    async def test_ready_arriving_during_actual_reset_cannot_repopulate_after_reset(self):
        await self.scan("demo-1")
        original = self.app._cancel_audio_completions
        entered, release = asyncio.Event(), asyncio.Event()
        async def gated(session_id, **kwargs):
            if kwargs.get("operation") == "reset":
                entered.set()
                await release.wait()
            return await original(session_id, **kwargs)
        self.app._cancel_audio_completions = gated
        resetting = asyncio.create_task(self.app.reset_session(self.boundary.session_id))
        draining = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await self.capture.scan("demo-2")
            draining = asyncio.create_task(self.drain())
            await asyncio.sleep(.01)
            self.assertFalse(draining.done())
            release.set()
            await asyncio.wait_for(asyncio.gather(resetting, draining), 2)
            self.assertFalse(self.app._canonical_profiles)
            self.assertIsNone(self.boundary.facts)
            self.assertIsNone(self.client.patient())
            self.assertNotIn(self.boundary.session_id, self.app.canonical_runtime.sessions)
        finally:
            release.set()
            await resetting
            if draining is not None:
                await draining
            self.app._cancel_audio_completions = original

    async def test_actual_urgent_reset_clears_card_and_preserves_incident(self):
        await self.scan("demo-1")
        await self.send("HandsetLifted")
        await self.send("VoiceIntentReady", {"intent": "free_speech", "safety_severity": "urgent"})
        incident = self.state().incident_id
        result = await self.app.reset_session(self.boundary.session_id)
        self.assertFalse(result["reset"])
        self.assertEqual(self.state().incident_id, incident)
        self.assertEqual(self.state().safety_severity, "urgent")
        self.assertIsNone(self.state().profile_ref)
        self.assertFalse(self.app._canonical_profiles)
        self.assertIsNone(self.boundary.facts)
        self.assertIsNone(self.client.patient())

    async def test_idle_termination_clears_all_profile_owners(self):
        await self.scan("demo-1")
        self.app.idle_timeout_seconds = 1
        results = await self.app.expire_idle_sessions(now=self.app.now() + 2)
        self.assertEqual(results[0]["reason"], "idle_timeout")
        self.assertIsNone(self.state().profile_ref)
        self.assertFalse(self.app._canonical_profiles)
        self.assertIsNone(self.boundary.facts)
        self.assertIsNone(self.client.patient())
        self.assertFalse(self.app._identity_scope_live(self.boundary.session_id))

    async def test_watchdog_cleanup_clears_all_profile_owners(self):
        await self.scan("demo-1")
        self.app.metrics.watchdog_check = lambda *args, **kwargs: {
            "stale_tasks": [self.boundary.session_id], "stale_playback": [], "stale_queue_generations": []}
        await self.app.watchdog_check_once()
        self.assertIsNone(self.state().profile_ref)
        self.assertFalse(self.app._canonical_profiles)
        self.assertIsNone(self.boundary.facts)
        self.assertIsNone(self.client.patient())

    async def test_guest_without_private_context_is_not_cancelled_by_scan_clear(self):
        await self.send("HandsetLifted")
        await self.scan("unknown")
        self.assertNotEqual(self.state().mode, "session_closed")
        self.assertEqual(self.state().profile_status, "anonymous")

    async def test_generic_ingress_cannot_submit_json_or_uid(self):
        for kind in ("ClientProfileReady", "ClientProfileCleared", "RfidPresented"):
            result = await self.send(kind, {"tag_id": "RAW", "patient_profile": {"identity": "forged"}})
            self.assertEqual(result["reason"], "unsupported_event" if kind == "RfidPresented" else "max_json_requires_private_client_boundary")
        with self.assertRaises(EventValidationError):
            parse_event({"type": "ClientProfileReady"}, "session", 1)
        self.assertFalse(self.app._canonical_profiles)

    async def test_ready_age_cannot_be_overwritten_by_age_button(self):
        await self.scan("demo-1")
        state = self.state()
        expected = state.age_years
        self.app.canonical_runtime._select_exhibition_age(state, 2, source="button", now=time.time())
        self.assertEqual(state.age_years, expected)
        self.assertEqual(state.age_band_source, "data14_age_years")

    async def test_unexpected_pump_cancel_closes_client_and_private_context(self):
        await self.scan("demo-1")
        task = asyncio.create_task(self.boundary.run())
        self.boundary._task = task
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.boundary._close_succeeded)
        self.assertIsNone(self.client.patient())
        self.assertIsNone(self.boundary.facts)
        self.assertFalse(self.app._canonical_profiles)

    async def test_new_scan_at_old_ready_expiry_is_not_drained_by_nested_expiry(self):
        await self.scan("demo-1")
        advanced = self.state().identity_ready_deadline + .1
        self.app.monotonic_now = lambda: advanced
        results = await self.scan("demo-2")
        self.assertIn("profile_ready", [x["reason"] for x in results])
        self.assertNotIn("stale_sequence", [x["reason"] for x in results])
        self.assertIsNotNone(self.client.patient())
        self.assertTrue(self.boundary.facts["wheelchair_access"])
        self.assertIsNotNone(self.state().profile_ref)

    async def test_ready_expiry_clears_client_facts_and_application(self):
        await self.scan("demo-1")
        self.app.monotonic_now = lambda: self.state().identity_ready_deadline + .1
        # Preserve a fixed advanced time while cleanup clears the state's deadline.
        advanced = self.app.monotonic_now()
        self.app.monotonic_now = lambda: advanced
        await self.app.expire_identity_leases()
        self.assertFalse(self.app._canonical_profiles)
        self.assertIsNone(self.client.patient())
        self.assertIsNone(self.boundary.facts)

    async def test_application_shutdown_owns_pump_and_client_close(self):
        await self.scan("demo-1")
        await self.app.shutdown_identity()
        self.assertIsNone(self.client.patient())
        self.assertIsNone(self.boundary.facts)
        self.assertTrue(self.boundary._closed)
        self.assertTrue(self.boundary._close_succeeded)
        self.assertIsNone(self.app._max_client_boundary)

    async def test_duplicate_handset_down_does_not_clear_only_client(self):
        await self.scan("demo-1")
        result = await self.send("HandsetPlaced")
        self.assertEqual(result["reason"], "handset_already_placed")
        self.assertIsNotNone(self.client.patient())
        self.assertIsNotNone(self.boundary.facts)
        self.assertIsNotNone(self.state().profile_ref)

    async def test_new_card_after_max_scan_started_is_not_cancelled_by_cleanup(self):
        await self.scan("demo-1")
        results = await self.scan("demo-2")
        self.assertIn("profile_ready", [x["reason"] for x in results])
        self.assertTrue(self.boundary.facts["wheelchair_access"])
        self.assertIsNotNone(self.client.patient())

    async def test_facts_stay_private_and_do_not_become_schedule_or_flags(self):
        await self.scan("demo-2")
        self.assertTrue(self.boundary.facts["wheelchair_access"])
        self.assertIn(self.boundary.facts["acquaintance"], {"first", "repeat"})
        profile = self.app._canonical_profiles[self.boundary.session_id]
        self.assertNotIn("schedule_ref", profile)
        self.assertNotIn("motor_flags", profile)
        self.assertNotIn("diagnoses", profile)
        self.assertNotIn("wheelchair_access", json.dumps(self.state().snapshot()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
