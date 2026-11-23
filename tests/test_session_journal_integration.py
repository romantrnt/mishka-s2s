"""Application journal wiring, synthetic visits/providers and private tmp roots."""
import asyncio
import json
import logging
from pathlib import Path
import threading

import pytest

from app.foundation.api import build_application
from app.foundation.age_policy import policy_for_band
from app.foundation.canonical_contract import CanonicalState
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.s2s import TurnResult
from test_contextual_game_flow import Model


class Harness:
    def __init__(self, root, enabled=True):
        self.root, self.sid, self.sequence = root, "synthetic-reused-session", 0
        self.app, *_ = build_application(settings=Settings(profile="mock", dialogue_journal_enabled=enabled,
            dialogue_journal_dir=str(root)), background_scheduler=False)
        self.install_dialogue()
        self.app.bounded_s2s.llm = Model('<say>Синтетический ответ о деревьях.</say>')

    def install_dialogue(self):
        self.app._cancel_welcome_timer(self.sid)
        self.app._invalidate_audio_lease(self.sid)
        self.app.canonical_runtime.sessions[self.sid] = CanonicalState(self.sid, mode="bear_dialogue", handset="up",
            age_band="4-5", age_years=4, age_band_source="exhibition_button",
            age_policy=policy_for_band("4-5", source="exhibition_button").as_dict())

    async def send(self, kind, text=None, payload=None):
        self.sequence += 1
        return await self.app.receive_async(Event(kind, self.sid, self.sequence, 1., "journal-test",
            payload or {}, str(self.sequence), float(self.sequence)), text)

    async def turn(self, text="Синтетическая реплика о деревьях."):
        return await self.send("SpeechReceived", text)

    async def finishes(self):
        await asyncio.gather(*tuple(self.app._journal_jobs))

    async def close(self):
        await self.app._cleanup_session_resources(self.sid)
        await self.app.shutdown_identity()


def logs(root):
    return [([json.loads(line) for line in path.read_text().splitlines()], path)
            for path in root.glob("*.jsonl")]


@pytest.mark.asyncio
async def test_enabled_records_only_admitted_dialogue_and_completed_validated_response(tmp_path):
    h = Harness(tmp_path / "journal")
    try:
        assert not h.root.exists()
        await h.turn()
        items, path = logs(h.root)[0]
        assert items == [{"role": "user", "text": "Синтетическая реплика о деревьях."},
                         {"role": "assistant", "text": "Синтетический ответ о деревьях."}]
        assert h.sid not in path.name
        assert not list(h.root.glob("*.summary.json"))
        await h.send("HandsetPlaced")
        assert not h.app._journal_visits
        await h.finishes()
        assert len(list(h.root.glob("*.summary.json"))) == 1
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_disabled_turn_boundary_shutdown_create_no_journal_io_or_jobs(tmp_path, monkeypatch, caplog):
    h = Harness(tmp_path / "absent", enabled=False)
    def forbidden(*args, **kwargs): raise AssertionError("disabled journal I/O")
    monkeypatch.setattr(h.app, "_journal_io", forbidden)
    caplog.set_level(logging.INFO)
    try:
        await h.turn()
        await h.send("HandsetPlaced")
        await h.close()
        assert h.app._dialogue_journal is None and not h.app._journal_visits and not h.app._journal_jobs
        assert h.app._journal_shutdown_task is None and not h.root.exists()
        assert "Синтетическая реплика" not in caplog.text and "Синтетический ответ" not in caplog.text
        assert not h.app.history.list_history()
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_reused_session_id_gets_distinct_visit_and_separate_files(tmp_path):
    h = Harness(tmp_path / "journal")
    try:
        await h.turn("Первая синтетическая реплика.")
        first = h.app._journal_visits[h.sid]["id"]
        await h.send("HandsetPlaced")
        await h.finishes()
        await h.send("HandsetLifted")
        h.install_dialogue()  # Isolate journal wiring from separately tested onboarding.
        await h.turn("Вторая синтетическая реплика.")
        second = h.app._journal_visits[h.sid]["id"]
        assert first != second and first != h.sid and second != h.sid
        await h.send("HandsetPlaced")
        await h.finishes()
        contents = [items for items, _ in logs(h.root)]
        assert len(contents) == 2
        assert sorted(items[0]["text"] for items in contents) == ["Вторая синтетическая реплика.", "Первая синтетическая реплика."]
        assert len(list(h.root.glob("*.summary.json"))) == 2
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_hangup_during_late_provider_cannot_record_into_next_visit(tmp_path):
    h = Harness(tmp_path / "journal")
    entered, release = asyncio.Event(), asyncio.Event()
    pending = None
    original = h.app.bounded_s2s.run_text_turn
    async def late(*args):
        entered.set()
        await release.wait()
        return TurnResult(("Старый синтетический ответ.",), (), False)
    h.app.bounded_s2s.run_text_turn = late
    try:
        pending = asyncio.create_task(h.turn("Старая синтетическая реплика."))
        await asyncio.wait_for(entered.wait(), 2)
        await h.send("HandsetPlaced")
        assert not h.app._journal_visits
        await h.finishes()
        await h.send("HandsetLifted")
        h.install_dialogue()
        h.app.bounded_s2s.run_text_turn = original
        await h.turn("Новая синтетическая реплика.")
        new_visit = h.app._journal_visits[h.sid]
        release.set()
        assert (await asyncio.wait_for(pending, 2))["reason"] == "stale_session_generation"
        assert h.app._journal_visits[h.sid] is new_visit
        await h.send("HandsetPlaced")
        await h.finishes()
        contents = [items for items, _ in logs(h.root)]
        old = next(items for items in contents if items[0]["text"].startswith("Старая"))
        new = next(items for items in contents if items[0]["text"].startswith("Новая"))
        assert len(old) == 1 and all("Старый" not in item["text"] for item in new)
    finally:
        release.set()
        if pending: await asyncio.gather(pending, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_count", [1, 2])
async def test_cancelled_ingress_write_is_drained_before_visit_summary(tmp_path, cancel_count):
    h = Harness(tmp_path / "journal")
    entered, release = threading.Event(), threading.Event()
    original = h.app._dialogue_journal.append_turn
    def blocked(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)
    h.app._dialogue_journal.append_turn = blocked
    pending = asyncio.create_task(h.turn())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        for _ in range(cancel_count):
            pending.cancel()
            await asyncio.sleep(0)
        await h.send("HandsetPlaced")
        assert not h.app._journal_visits
        release.set()
        with pytest.raises(asyncio.CancelledError): await pending
        await h.finishes()
        assert len(logs(h.root)[0][0]) == 1
        summary = json.loads(next(h.root.glob("*.summary.json")).read_text())
        assert summary["turn_count"] == 1
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
async def test_raw_provider_exception_is_type_only_log_and_not_assistant_text(tmp_path, caplog):
    h = Harness(tmp_path / "journal")
    async def fail(*args): raise OSError("SYNTHETIC_PRIVATE_PROVIDER_BODY")
    h.app.bounded_s2s.run_text_turn = fail
    try:
        await h.turn()
        assert "bounded dialogue turn failed: OSError" in caplog.text
        assert "SYNTHETIC_PRIVATE_PROVIDER_BODY" not in caplog.text
        assert not any(record.exc_info for record in caplog.records if "bounded dialogue" in record.getMessage())
        assert logs(h.root)[0][0] == [{"role": "user", "text": "Синтетическая реплика о деревьях."}]
        assert "SYNTHETIC_PRIVATE_PROVIDER_BODY" not in repr(h.app.audio_runtime_diagnostics)
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_untrusted_input_and_profile_onboarding_do_not_create_journal(tmp_path):
    h = Harness(tmp_path / "journal")
    try:
        await h.send("SpeechReceived", "Синтетическая реплика.", {"_stt_untrusted": True})
        assert not h.root.exists() and not h.app._journal_visits
        h.app.canonical_runtime.sessions[h.sid].mode = "exhibition_name_capture"
        await h.send("SpeechReceived", "Синтетическое имя.")
        assert not h.root.exists() and not h.app._journal_visits
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_resource_cleanup_does_not_end_visit_but_direct_reset_does(tmp_path):
    h = Harness(tmp_path / "journal")
    try:
        await h.turn()
        visit = h.app._journal_visits[h.sid]
        await h.app._cleanup_session_resources(h.sid)
        assert h.app._journal_visits[h.sid] is visit and not visit["closed"]
        assert not list(h.root.glob("*.summary.json"))
        await h.app.reset_session(h.sid)
        assert not h.app._journal_visits and visit["closed"]
        await h.finishes()
        assert len(list(h.root.glob("*.summary.json"))) == 1
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_shutdown_finishes_active_visit_and_denies_late_recording(tmp_path):
    h = Harness(tmp_path / "journal")
    try:
        await h.turn()
        await h.app.shutdown_identity()
        assert not h.app._journal_visits and not h.app._dialogue_journal._sessions
        assert len(list(h.root.glob("*.summary.json"))) == 1
        assert h.app._journal_visit(h.sid) is None
    finally:
        await h.close()


def test_sync_shutdown_without_event_loop_closes_journal(tmp_path):
    h = Harness(tmp_path / "journal")
    assert h.app._dialogue_journal.append_turn("synthetic-direct-visit", "user", "Синтетическая реплика.")
    h.app.cancel_identity_work()
    assert len(list(h.root.glob("*.summary.json"))) == 1
    assert h.app._dialogue_journal._closed and not h.app._journal_visits


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["HardwareDisconnected", "AnonymousStart", "panel_end"])
async def test_other_accepted_visit_boundaries_finish_journal(tmp_path, boundary):
    h = Harness(tmp_path / "journal")
    try:
        await h.turn()
        if boundary == "panel_end":
            await h.app.session_action(h.sid, "end", ownership_guard=lambda: True)
        else:
            await h.send(boundary)
        assert not h.app._journal_visits
        await h.finishes()
        assert len(list(h.root.glob("*.summary.json"))) == 1
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_rejected_start_and_duplicate_boundary_do_not_finish_current_visit(tmp_path):
    h = Harness(tmp_path / "journal")
    try:
        await h.turn()
        visit = h.app._journal_visits[h.sid]
        assert (await h.send("SessionStarted"))["reason"] == "session_action_untrusted"
        # Duplicate sequence must fail admission before journal revocation.
        rejected = await h.app.receive_async(Event("HandsetPlaced", h.sid, 1, 1., "journal-test", {}, "duplicate", 1.))
        assert h.app._journal_visits[h.sid] is visit and not visit["closed"]
        assert not list(h.root.glob("*.summary.json"))
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_shutdown_revokes_before_client_close_await_and_drains_on_client_failure(tmp_path):
    from types import SimpleNamespace
    h = Harness(tmp_path / "journal")
    entered, release = asyncio.Event(), asyncio.Event()
    async def blocked_close():
        entered.set()
        await release.wait()
        raise OSError("synthetic client close failure")
    closing = None
    try:
        await h.turn()
        h.app.max_json = True
        h.app._max_client_boundary = SimpleNamespace(session_id="another-synthetic-id", close=blocked_close,
                                                    clear_client=lambda: None)
        closing = asyncio.create_task(h.app.shutdown_identity())
        await asyncio.wait_for(entered.wait(), 2)
        assert not h.app._journal_visits and h.app._identity_shutdown
        release.set()
        with pytest.raises(OSError, match="synthetic client close failure"):
            await closing
        assert h.app._dialogue_journal._closed
        assert len(list(h.root.glob("*.summary.json"))) == 1
    finally:
        release.set()
        if closing: await asyncio.gather(closing, return_exceptions=True)
        h.app.max_json = False
        h.app._max_client_boundary = None
        await h.close()


@pytest.mark.asyncio
async def test_cancel_resistant_provider_cannot_record_cancelled_response(tmp_path):
    h = Harness(tmp_path / "journal")
    entered = asyncio.Event()
    async def late(*args):
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            return TurnResult(("Отменённый синтетический ответ.",), (), False)
    h.app.bounded_s2s.run_text_turn = late
    pending = asyncio.create_task(h.turn())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        pending.cancel()
        await asyncio.wait_for(pending, 2)
        assert logs(h.root)[0][0] == [{"role": "user", "text": "Синтетическая реплика о деревьях."}]
    finally:
        await asyncio.gather(pending, return_exceptions=True)
        await h.close()
