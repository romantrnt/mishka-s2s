"""Synthetic isolated journal storage only; never a production journal root."""
import concurrent.futures
import json
import os
from pathlib import Path
import re
import stat

import pytest

from app.foundation import session_journal as module
from app.foundation.session_journal import SessionJournal


def files(root, suffix):
    return sorted(root.glob("*" + suffix))


def test_disabled_has_no_storage_random_key_or_retained_turns(tmp_path, monkeypatch, caplog, capsys):
    root = tmp_path / "absent"
    def forbidden(*args, **kwargs): raise AssertionError("disabled storage operation")
    monkeypatch.setattr(module.os, "open", forbidden)
    monkeypatch.setattr(module.os, "mkdir", forbidden)
    monkeypatch.setattr(module.secrets, "token_bytes", forbidden)
    journal = SessionJournal(root=root)
    for _ in range(3):
        assert journal.append_turn("synthetic-session", "user", "Синтетическая реплика") is False
        assert journal.finish("synthetic-session") is False
    assert journal.close() is False
    assert not journal._sessions and journal._key is None and journal._root is None
    assert not root.exists() and not caplog.records
    assert capsys.readouterr() == ("", "")


def test_enabled_writes_only_two_private_files_and_truthful_deterministic_extracts(tmp_path):
    root = tmp_path / "journal"
    journal = SessionJournal(root=root, enabled=True)
    assert not root.exists()  # Lazy even when enabled.
    turns = [("user", "Синтетический вопрос о лесе."), ("assistant", "В лесу растут деревья.")]
    for role, text in turns: assert journal.append_turn("synthetic-session-with-name", role, text)
    assert journal.finish("synthetic-session-with-name")
    paths = list(root.iterdir())
    assert len(paths) == 2
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    for path in paths:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert re.fullmatch(r"[0-9a-f]{64}\.(jsonl|summary\.json)", path.name)
    assert [json.loads(line) for line in files(root, ".jsonl")[0].read_text().splitlines()] == [
        {"role": role, "text": text} for role, text in turns]
    summary_path = files(root, ".summary.json")[0]
    summary = json.loads(summary_path.read_text())
    assert summary == {"schema_version": 1, "kind": "extractive", "turn_count": 2,
                       "excerpts": [{"role": role, "text": text} for role, text in turns]}
    original = summary_path.read_bytes()
    assert journal.finish("synthetic-session-with-name")
    assert summary_path.read_bytes() == original
    assert not journal.append_turn("synthetic-session-with-name", "user", "Поздняя реплика")
    assert not next(iter(journal._sessions.values()))["turns"]
    assert journal.close()


def test_session_filenames_are_unlinkable_across_runtime_keys(tmp_path):
    names = []
    for index in range(2):
        root = tmp_path / str(index)
        journal = SessionJournal(root=root, enabled=True)
        assert journal.append_turn("same-synthetic-id", "user", "Тест")
        names.append(files(root, ".jsonl")[0].name)
        assert journal.close()
    assert names[0] != names[1]


def test_caps_reject_extra_data_and_summary_is_short(tmp_path):
    journal = SessionJournal(root=tmp_path / "journal", enabled=True)
    journal.MAX_SESSIONS, journal.MAX_TURNS, journal.MAX_TEXT_CHARS = 2, 8, 20
    for index in range(8):
        assert journal.append_turn("one", "user", f"Тестовая фраза {index}.")
    assert not journal.append_turn("one", "user", "Лишняя фраза")
    assert not journal.append_turn("two", "assistant", "x" * 21)
    assert journal.append_turn("two", "assistant", "Короткая фраза")
    assert not journal.append_turn("three", "user", "Новая фраза")
    assert journal.finish("one") and journal.close()
    summaries = [json.loads(path.read_text()) for path in files(journal._root, ".summary.json")]
    long = next(item for item in summaries if item["turn_count"] == 8)
    assert [item["text"] for item in long["excerpts"]] == [f"Тестовая фраза {i}." for i in (0, 1, 2, 5, 6, 7)]
    assert len(long["excerpts"]) == 6


def test_invalid_input_creates_nothing_and_logs_nothing(tmp_path, caplog, capsys):
    root = tmp_path / "journal"
    journal = SessionJournal(root=root, enabled=True)
    for sid, role, text in [(None, "user", "Тест"), ("", "user", "Тест"), ("s" * 513, "user", "Тест"),
                            ("one", "system", "Тест"), ("one", "tool", "Тест"),
                            ("one", "assistant", "  "), ("one", "user", None)]:
        assert not journal.append_turn(sid, role, text)
    assert not root.exists() and not journal._sessions
    assert not caplog.records and capsys.readouterr() == ("", "")
    assert journal.close()


@pytest.mark.parametrize("parent_link", [False, True])
def test_symlink_directory_never_receives_data(tmp_path, parent_link):
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    journal = SessionJournal(root=link / "journal" if parent_link else link, enabled=True)
    assert not journal.append_turn("synthetic", "user", "Тест")
    assert list(target.iterdir()) == []
    journal.close()


def test_existing_nonprivate_directory_is_rejected_without_chmod(tmp_path):
    root = tmp_path / "journal"
    root.mkdir(mode=0o755)
    journal = SessionJournal(root=root, enabled=True)
    assert not journal.append_turn("synthetic", "user", "Тест")
    assert stat.S_IMODE(root.stat().st_mode) == 0o755 and not list(root.iterdir())
    journal.close()


def test_symlink_log_cannot_modify_another_file(tmp_path):
    root = tmp_path / "journal"
    journal = SessionJournal(root=root, enabled=True)
    assert journal.append_turn("synthetic", "user", "Тест")
    log = files(root, ".jsonl")[0]
    outside = tmp_path / "outside"
    outside.write_text("original")
    log.unlink()
    log.symlink_to(outside)
    assert not journal.append_turn("synthetic", "assistant", "Вторая реплика")
    assert outside.read_text() == "original"
    journal.close()


def test_failed_atomic_summary_leaves_no_partial_and_can_retry(tmp_path, monkeypatch, caplog, capsys):
    root = tmp_path / "journal"
    journal = SessionJournal(root=root, enabled=True)
    assert journal.append_turn("synthetic", "user", "Тест")
    def fail(*args, **kwargs): raise OSError("synthetic storage failure")
    with monkeypatch.context() as patch:
        patch.setattr(module.os, "replace", fail)
        assert not journal.finish("synthetic")
    assert len(list(root.iterdir())) == 1 and not files(root, ".tmp")
    assert journal.finish("synthetic") and journal.close()
    assert not caplog.records and capsys.readouterr() == ("", "")


def test_failed_turn_write_rolls_back_and_retry_does_not_duplicate(tmp_path, monkeypatch):
    root = tmp_path / "journal"
    journal = SessionJournal(root=root, enabled=True)
    original = module.os.write
    def partial_then_fail(descriptor, payload):
        original(descriptor, payload[:2])
        raise OSError("synthetic partial write")
    with monkeypatch.context() as patch:
        patch.setattr(module.os, "write", partial_then_fail)
        assert not journal.append_turn("synthetic", "user", "Тест")
    assert not list(root.iterdir()) and not journal._sessions
    assert journal.append_turn("synthetic", "user", "Тест")
    before = files(root, ".jsonl")[0].read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(module.os, "write", partial_then_fail)
        assert not journal.append_turn("synthetic", "assistant", "Ответ")
    assert files(root, ".jsonl")[0].read_bytes() == before
    assert journal.append_turn("synthetic", "assistant", "Ответ")
    assert journal.close()
    assert len(files(root, ".jsonl")[0].read_text().splitlines()) == 2


def test_close_finishes_all_and_parallel_turns_remain_valid_jsonl(tmp_path):
    root = tmp_path / "journal"
    journal = SessionJournal(root=root, enabled=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        accepted = list(pool.map(lambda index: journal.append_turn("synthetic", "user", f"Тест {index}"), range(20)))
    assert all(accepted)
    assert journal.append_turn("other", "assistant", "Ещё одна фраза")
    assert journal.close()
    assert len(list(root.iterdir())) == 4
    assert sorted(json.loads(path.read_text())["turn_count"] for path in files(root, ".summary.json")) == [1, 20]
    assert not journal._sessions and journal._key is None and journal._directory_fd is None
    assert not journal.append_turn("late", "user", "Поздняя реплика")
    assert not journal.finish("late") and journal.close() is False


def test_fifo_replacement_fails_promptly_without_writing_summary(tmp_path):
    root = tmp_path / "journal"
    journal = SessionJournal(root=root, enabled=True)
    assert journal.append_turn("synthetic", "user", "Тест")
    log = files(root, ".jsonl")[0]
    log.unlink()
    os.mkfifo(log, mode=0o600)
    assert not journal.append_turn("synthetic", "assistant", "Ответ")
    assert not journal.finish("synthetic")
    assert not files(root, ".summary.json")
    assert journal.close() is False


def test_summary_symlink_is_rejected_and_excerpt_is_bounded(tmp_path):
    root = tmp_path / "journal"
    journal = SessionJournal(root=root, enabled=True)
    text = "Синтетическая фраза. " * 30
    assert journal.append_turn("synthetic", "user", text)
    log = files(root, ".jsonl")[0]
    summary = root / (log.stem + ".summary.json")
    outside = tmp_path / "outside"
    outside.write_text("original")
    summary.symlink_to(outside)
    assert not journal.finish("synthetic")
    assert outside.read_text() == "original" and not files(root, ".tmp")
    summary.unlink()
    assert journal.finish("synthetic")
    content = json.loads(summary.read_text())
    assert content["excerpts"][0]["text"] == " ".join(text.split())[:160] + "…"
    assert journal.close()


def test_fresh_nested_layout_creates_only_private_missing_directories(tmp_path):
    root = tmp_path / "runtime" / "private" / "session-journal"
    journal = SessionJournal(root=root, enabled=True)
    assert journal.append_turn("synthetic-visit", "user", "Тестовая реплика")
    for path in (root.parent.parent, root.parent, root):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    assert journal.finish("synthetic-visit") and journal.close()


def test_nested_creation_does_not_follow_intermediate_symlink(tmp_path):
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    (tmp_path / "runtime").symlink_to(target, target_is_directory=True)
    journal = SessionJournal(root=tmp_path / "runtime" / "private" / "session-journal", enabled=True)
    assert not journal.append_turn("synthetic-visit", "user", "Тестовая реплика")
    assert list(target.iterdir()) == []
    journal.close()


def test_failed_partial_append_and_failed_rollback_poison_visit(tmp_path, monkeypatch):
    journal = SessionJournal(root=tmp_path / "journal", enabled=True)
    assert journal.append_turn("synthetic", "user", "Первая тестовая реплика")
    real_write = module.os.write
    def partial_failure(descriptor, payload):
        real_write(descriptor, payload[:2])
        raise OSError("synthetic storage failure")
    def rollback_failure(*args):
        raise OSError("synthetic rollback failure")
    with monkeypatch.context() as patch:
        patch.setattr(journal, "_write", partial_failure)
        patch.setattr(module.os, "ftruncate", rollback_failure)
        assert not journal.append_turn("synthetic", "assistant", "Неполная тестовая реплика")
    assert not journal.append_turn("synthetic", "user", "Поздняя тестовая реплика")
    assert not journal.finish("synthetic")
    assert not files(journal._root, ".summary.json")
    assert not journal.close()
