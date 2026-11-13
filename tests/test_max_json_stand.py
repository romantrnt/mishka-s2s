"""Max HTTPS/SQLite → JSON boundary → Stand ASGI/PTY and real lifespan.

The positive case explicitly assumes only control-plane readiness, exactly as
the existing RFID HTTP acceptance does. /ready remains503, production false.
"""
import asyncio
import importlib.util
import os
import json
import sys
import tempfile
import threading
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
from phone_local_demo import DemoServer, initialize, connect_demo
from fastapi.testclient import TestClient
from app.foundation.config import Settings
from app.foundation.lifecycle import RuntimeState
from app.foundation.stand import create_stand_app
from test_max_json_integration import Capture

TOKEN = "synthetic-test-only-token"
HEADERS = {"x-stand-token": TOKEN}


async def wait_state(api, mode):
    app = api.state.stand.application
    for _ in range(150):
        state = app.canonical_runtime.sessions.get("stand")
        if state is not None and state.mode == mode:
            return
        await asyncio.sleep(.02)
    raise AssertionError("stand_state_timeout")


async def assume_control_ready(api):
    lifecycle = api.state.lifecycle
    await asyncio.wait_for(asyncio.shield(lifecycle._warmup), 3)
    assert not lifecycle.admits_users()
    assert lifecycle.snapshot()["state"] == "DEGRADED"
    lifecycle._state = RuntimeState.READY
    lifecycle._reason = "test_only_assumed_control_plane_readiness"


class StandJsonTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="mishka-stand-json-")
        cls.directory = initialize(Path(cls.temp.name) / "synthetic")
        cls.server = DemoServer(cls.directory, port=0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.thread.join(5); cls.server.server_close(); cls.temp.cleanup()
    def create(self):
        capture = Capture()
        phone = connect_demo(self.directory, port=self.server.server_address[1], capture_runner=capture)
        api = create_stand_app(Settings(profile="mock", hardware_mode="simulator",
            runtime_mode="exhibition", identity_mode="max_json",
            stand_access_token=TOKEN), phone_client=phone)
        return api, phone, capture

    def test_given_control_ready_json_lift_hangup_and_lifespan_are_wired(self):
        api, phone, capture = self.create()
        stand = api.state.stand
        boundary = stand.application._max_client_boundary
        self.assertEqual(boundary.session_id, stand.active_session_id)
        self.assertNotIn("/stand/rfid", [getattr(r, "path", None) for r in api.routes])
        with TestClient(api) as http:
            http.portal.call(assume_control_ready, api)
            self.assertEqual(http.get("/ready").status_code, 503)
            http.portal.call(capture.scan, "demo-1")
            http.portal.call(wait_state, api, "profile_ready")
            public = http.get("/stand/state", headers=HEADERS).json()
            self.assertNotIn("Демо", json.dumps(public, ensure_ascii=False))
            self.assertEqual(http.post("/stand/handset", json={"state": "up"}).status_code, 401)
            up = http.post("/stand/handset", json={"state": "up", "session_id": "stand"}, headers=HEADERS)
            self.assertEqual(up.status_code, 200)
            self.assertEqual(up.json()["reason"], "exhibition_handset_lifted")
            self.assertEqual(stand.application.canonical_runtime.sessions["stand"].profile_status, "identified")
            down = http.post("/stand/handset", json={"state": "down", "session_id": "stand"}, headers=HEADERS)
            self.assertEqual(down.status_code, 200)
            self.assertIsNone(phone.patient())
            self.assertFalse(stand.application._canonical_profiles)
        self.assertTrue(boundary._closed)
        self.assertTrue(boundary._close_succeeded)
        self.assertTrue(stand._closed)

    def test_actual_reset_route_clears_owned_card_and_rejects_foreign_cursor(self):
        api, phone, capture = self.create()
        stand = api.state.stand
        boundary = stand.application._max_client_boundary
        with TestClient(api) as http:
            http.portal.call(assume_control_ready, api)
            http.portal.call(capture.scan, "demo-1")
            http.portal.call(wait_state, api, "profile_ready")
            reference = stand.application.canonical_runtime.sessions["stand"].profile_ref
            foreign = http.post("/stand/reset", json={"session_id": "foreign"}, headers=HEADERS)
            self.assertEqual(foreign.status_code, 422)
            self.assertEqual(stand.active_session_id, boundary.session_id)
            self.assertEqual(stand.application.canonical_runtime.sessions["stand"].profile_ref, reference)
            self.assertIsNotNone(phone.patient())
            reset = http.post("/stand/reset", json={"session_id": "stand"}, headers=HEADERS)
            self.assertEqual(reset.status_code, 200)
            self.assertTrue(reset.json()["reset"])
            self.assertFalse(stand.application._canonical_profiles)
            self.assertIsNone(boundary.facts)
            self.assertIsNone(phone.patient())
            http.portal.call(capture.scan, "demo-2")
            http.portal.call(wait_state, api, "profile_ready")
            lifted = http.post("/stand/handset", json={"state": "up"}, headers=HEADERS)
            self.assertEqual(lifted.status_code, 200)
            self.assertEqual(lifted.json()["reason"], "exhibition_handset_lifted")

    def test_failed_client_close_does_not_skip_other_owner_cleanup(self):
        api, phone, capture = self.create()
        stand = api.state.stand
        boundary = stand.application._max_client_boundary
        original_close = phone.close
        manager = stand.application.runtime_manager
        original_manager_close = manager.close
        manager_closed = []
        async def close_manager():
            manager_closed.append(True)
            await original_manager_close()
        manager.close = close_manager
        phone.close = lambda: False
        try:
            with self.assertRaises(BaseExceptionGroup):
                with TestClient(api):
                    pass
            self.assertTrue(manager_closed)
            self.assertTrue(stand._closed)
            self.assertIsNone(stand.application._watchdog_task)
            self.assertIsNone(stand.application._idle_monitor_task)
            self.assertIs(stand.application._max_client_boundary, boundary)
            self.assertFalse(boundary._close_succeeded)
        finally:
            phone.close = original_close
            self.assertTrue(phone.close())

    def test_default_mock_admission_blocks_profile_and_closes_client(self):
        api, phone, capture = self.create()
        boundary = api.state.stand.application._max_client_boundary
        with TestClient(api) as http:
            http.portal.call(capture.scan, "demo-1")
            http.portal.call(asyncio.sleep, .2)
            self.assertEqual(http.get("/ready").status_code, 503)
            self.assertFalse(api.state.stand.application._canonical_profiles)
            self.assertIsNone(phone.patient())
        self.assertTrue(boundary._close_succeeded)


if __name__ == "__main__":
    unittest.main(verbosity=2)
