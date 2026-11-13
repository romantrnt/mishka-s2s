"""Synthetic launcher: real Max TLS/SQLite, actual Stand lifespan and shutdown."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from scripts.run_max_json_demo import DemoError, SyntheticCapture, demo_session, load_max_source

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "run_max_json_demo.py"
# Tests explicitly pass the selected checkout to the launcher. Nothing falls
# back to another checkout or downloads Max when this input is absent.
MAX_SRC = Path(os.environ.get("MAX_JSON_TEST_SRC", ROOT.parent / "max_files" / "src")).resolve()
HAS_MAX = (MAX_SRC / "phone_local_demo.py").is_file()


def json_events(output):
    return [json.loads(line) for line in output.splitlines() if line.startswith("{")]


class LauncherCliTests(unittest.TestCase):
    def cli(self, commands, *flags):
        completed = subprocess.run([sys.executable, str(LAUNCHER), "--max-src", str(MAX_SRC), *flags],
                                   cwd=ROOT, input=commands, text=True, capture_output=True, timeout=35)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(completed.stderr, "")
        return json_events(completed.stdout), completed.stdout

    def assert_shutdown(self, event):
        self.assertEqual(event["event"], "shutdown")
        for key in ("client_closed", "stand_closed", "capture_stopped", "server_stopped", "context_cleared"):
            self.assertIs(event[key], True, key)
        self.assertIs(event["production_ready"], False)

    def test_help_needs_neither_max_source_nor_runtime_initialization(self):
        completed = subprocess.run([sys.executable, "-S", str(LAUNCHER), "--help"],
                                   input="", text=True, capture_output=True, timeout=5)
        self.assertEqual(completed.returncode, 0)
        self.assertIn("--max-src", completed.stdout)
        self.assertIn("TEST-ONLY", completed.stdout)
        self.assertIn("production_ready=false", completed.stdout)

    def test_source_is_explicit_and_physical_mode_is_unavailable(self):
        for arguments in ([], ["--max-src", str(MAX_SRC), "--hardware-mode", "physical"]):
            completed = subprocess.run([sys.executable, str(LAUNCHER), *arguments],
                                       input="", text=True, capture_output=True, timeout=5)
            self.assertEqual(completed.returncode, 2)
        with self.assertRaises(DemoError):
            load_max_source(ROOT / "not-a-max-source")

    @unittest.skipUnless(HAS_MAX, "Explicit local Max source fixture is absent")
    def test_default_is_honest_and_eof_shuts_down(self):
        events, _ = self.cli("demo-1\nlift\nstatus\n")
        self.assertFalse(events[0]["control_admits_users"])
        self.assertEqual(events[0]["control_readiness"], "actual")
        self.assertEqual(events[0]["startup_lifecycle"]["state"], "DEGRADED")
        self.assertEqual(events[1]["command"], "demo-1")
        self.assertFalse(events[1]["context_present"])
        self.assertEqual(events[2]["reason"], "runtime_unavailable")
        self.assertEqual(events[2]["handset"], "down")
        self.assert_shutdown(events[-1])

    @unittest.skipUnless(HAS_MAX, "Explicit local Max source fixture is absent")
    def test_full_positive_cli_and_quit_shutdown(self):
        events, output = self.cli("demo-1\nlift\nhangup\ndemo-2\nunknown\ndemo-1\narchive\nquit\n",
                                  "--assume-control-ready")
        by_command = [event for event in events if event["event"] == "command"]
        self.assertTrue(by_command[0]["context_present"])
        self.assertEqual(by_command[0]["mode"], "profile_ready")
        self.assertEqual(by_command[1]["handset"], "up")
        self.assertEqual(by_command[1]["profile_status"], "identified")
        self.assertEqual(by_command[2]["handset"], "down")
        self.assertFalse(by_command[2]["context_present"])
        self.assertTrue(by_command[3]["context_present"])
        self.assertFalse(by_command[4]["context_present"])
        self.assertTrue(by_command[5]["context_present"])
        self.assertFalse(by_command[6]["context_present"])
        for event in events:
            self.assertIs(event["production_ready"], False)
            if event["event"] != "shutdown":
                self.assertEqual(event["control_readiness"], "TEST-ONLY assumed")
                self.assertEqual(event["readiness_http_status"], 503)
                self.assertFalse(event["software_runtime_usable"])
        for private in ("Демо", "patient_profile", "uid_lookup", "demo_credential", "first_name", "birth_year"):
            self.assertNotIn(private, output)
        self.assert_shutdown(events[-1])

    @unittest.skipUnless(HAS_MAX and os.name == "posix", "POSIX and local Max source required")
    def test_signal_with_partial_stdin_still_closes_every_owner(self):
        for signum in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=signum):
                process = subprocess.Popen([sys.executable, str(LAUNCHER), "--max-src", str(MAX_SRC)],
                                           cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE)
                try:
                    with selectors.DefaultSelector() as selector:
                        selector.register(process.stdout, selectors.EVENT_READ)
                        self.assertTrue(selector.select(20), "launcher_start_timeout")
                        started = json.loads(process.stdout.readline())
                    self.assertEqual(started["event"], "started")
                    process.stdin.write(b"stat")
                    process.stdin.flush()
                    time.sleep(0.05)
                    process.send_signal(signum)
                    stdout, stderr = process.communicate(timeout=20)
                    self.assertEqual(process.returncode, 0, stdout + stderr)
                    self.assertEqual(stderr, b"")
                    self.assert_shutdown(json_events(stdout.decode())[-1])
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate(timeout=5)


@unittest.skipUnless(HAS_MAX, "Explicit local Max source fixture is absent")
class LauncherBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_capture_and_handset_use_real_owned_stand_without_hardware(self):
        load_max_source(MAX_SRC)
        from ubuntu_z2 import UbuntuZ2Capture
        from app.foundation import api as foundation_api
        with patch.object(UbuntuZ2Capture, "__call__", side_effect=AssertionError("physical_capture_forbidden")), \
             patch.object(foundation_api, "_silero_loader", side_effect=AssertionError("model_load_forbidden")), \
             patch.object(foundation_api, "_whisper_loader", side_effect=AssertionError("model_load_forbidden")):
            async with demo_session(MAX_SRC, assume_control_ready=True, hardware_mode="fake") as runtime:
                self.assertEqual(runtime.boundary.session_id, runtime.stand.active_session_id)
                self.assertNotIn("/stand/rfid", [getattr(route, "path", None) for route in runtime.api.routes])
                real_inject = runtime.stand.inject
                calls = []
                async def observed_inject(kind, payload, session_id=None, **options):
                    calls.append((kind, payload, session_id))
                    return await real_inject(kind, payload, session_id, **options)
                runtime.stand.inject = observed_inject
                await runtime.command("demo-1")
                # Actual JSON first name and age reached the canonical runtime,
                # but launcher status exposes neither field nor the full card.
                state = runtime.stand.application.canonical_runtime.sessions[runtime.boundary.session_id]
                self.assertEqual(state.display_name, "Демо")
                self.assertEqual(state.age_band_source, "data14_age_years")
                self.assertFalse(getattr(runtime.stand.application, "_identity_lookup_tasks", {}))
                self.assertFalse(getattr(runtime.stand.application, "_identity_provider_tasks", {}))
                await runtime.command("lift")
                await runtime.command("hangup")
                self.assertEqual(calls, [("handset", {"state": "up"}, runtime.boundary.session_id),
                                         ("handset", {"state": "down"}, runtime.boundary.session_id)])
                self.assertFalse(runtime.stand.application._canonical_profiles)
                self.assertIsNone(runtime.boundary.facts)
            self.assertTrue(all(value for key, value in runtime.shutdown_status().items()
                                if key in {"client_closed", "stand_closed", "capture_stopped", "server_stopped", "context_cleared"}))

    async def test_duplicate_scan_is_reported_without_upstream_repair(self):
        async with demo_session(MAX_SRC, assume_control_ready=True) as runtime:
            self.assertTrue((await runtime.command("demo-1"))["context_present"])
            duplicate = await runtime.command("demo-1")
            self.assertEqual(duplicate["reason"], "duplicate_suppressed_by_max")
            self.assertFalse(duplicate["context_present"])

    async def test_capture_accepts_only_named_synthetic_codes(self):
        capture = SyntheticCapture(lambda code: self.fail("invalid_code_reached_frame_factory"))
        with self.assertRaises(DemoError):
            await capture.scan("arbitrary-raw-input", 0.1)
        with self.assertRaises(DemoError):
            async with demo_session(MAX_SRC, hardware_mode="physical"):
                self.fail("physical_mode_started")


if __name__ == "__main__":
    unittest.main(verbosity=2)
