"""Opt-in, bounded local turn journal; disabled instances perform no storage work."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
import threading


class SessionJournal:
    MAX_SESSIONS = 32
    MAX_TURNS = 128
    MAX_TEXT_CHARS = 2000
    SUMMARY_EXCERPTS = 6
    SUMMARY_EXCERPT_CHARS = 160

    def __init__(self, *, root: Path, enabled: bool = False):
        self.enabled = enabled is True
        self._root = Path(root) if self.enabled else None
        self._key = secrets.token_bytes(32) if self.enabled else None
        self._sessions: dict[str, dict] = {}
        self._directory_fd = None
        self._closed = False
        self._lock = threading.RLock()

    def _session_key(self, session_id):
        if not isinstance(session_id, str) or not session_id or len(session_id) > 512:
            return None
        return hmac.new(self._key, session_id.encode("utf-8"), hashlib.sha256).hexdigest()

    def _directory(self):
        if self._directory_fd is not None:
            return self._directory_fd
        # Walk directory descriptors so no parent or final symlink is followed.
        parts = Path(os.path.abspath(self._root)).parts
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        descriptor = os.open(parts[0], flags)
        try:
            for part in parts[1:]:
                try:
                    next_descriptor = os.open(part, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass  # Re-open with O_NOFOLLOW after a concurrent creator.
                    next_descriptor = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
            info = os.fstat(descriptor)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise PermissionError("journal_directory_not_private")
            self._directory_fd = descriptor
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _write(descriptor, payload):
        pending = memoryview(payload)
        while pending:
            written = os.write(descriptor, pending)
            if written <= 0:
                raise OSError("journal_write_incomplete")
            pending = pending[written:]
        os.fsync(descriptor)

    def append_turn(self, session_id: str, role: str, text: str) -> bool:
        if not self.enabled or self._closed:
            return False
        with self._lock:
            created = False
            session = None
            try:
                key = self._session_key(session_id)
                if (key is None or not isinstance(role, str) or role not in ("user", "assistant") or not isinstance(text, str)
                        or not text.strip() or len(text) > self.MAX_TEXT_CHARS):
                    return False
                session = self._sessions.get(key)
                if session is None and len(self._sessions) >= self.MAX_SESSIONS:
                    return False
                if session is not None and (session["finished"] or session.get("poisoned") or len(session["turns"]) >= self.MAX_TURNS):
                    return False
                turn = {"role": role, "text": text}
                payload = (json.dumps(turn, ensure_ascii=False) + "\n").encode("utf-8")
                directory = self._directory()
                filename = key + ".jsonl"
                flags = os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
                flags |= (os.O_CREAT | os.O_EXCL) if session is None else os.O_APPEND
                descriptor = os.open(filename, flags, 0o600, dir_fd=directory)
                created = session is None
                try:
                    info = os.fstat(descriptor)
                    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                        raise PermissionError("journal_file_not_private")
                    try:
                        self._write(descriptor, payload)
                    except OSError:
                        try:
                            os.ftruncate(descriptor, info.st_size)
                        except OSError:
                            if session is not None:
                                session["poisoned"] = True
                        raise
                finally:
                    os.close(descriptor)
                if session is None:
                    session = self._sessions[key] = {"turns": [], "finished": False}
                session["turns"].append(turn)
                return True
            except (OSError, TypeError, ValueError, UnicodeError):
                if created:
                    try:
                        os.unlink(filename, dir_fd=directory)
                    except OSError:
                        pass
                return False

    def _summary(self, turns):
        # Extracts only recorded words; no inferred facts or clinical analysis.
        half = self.SUMMARY_EXCERPTS // 2
        selected = turns if len(turns) <= self.SUMMARY_EXCERPTS else turns[:half] + turns[-half:]
        excerpts = []
        for turn in selected:
            text = " ".join(turn["text"].split())
            if len(text) > self.SUMMARY_EXCERPT_CHARS:
                text = text[:self.SUMMARY_EXCERPT_CHARS] + "…"
            excerpts.append({"role": turn["role"], "text": text})
        return {"schema_version": 1, "kind": "extractive", "turn_count": len(turns), "excerpts": excerpts}

    def finish(self, session_id: str) -> bool:
        if not self.enabled or self._closed:
            return False
        with self._lock:
            try:
                key = self._session_key(session_id)
                return bool(key and self._finish(key))
            except (OSError, TypeError, ValueError, UnicodeError):
                return False

    def _finish(self, key):
        session = self._sessions.get(key)
        if session is None or session.get("poisoned"):
            return False
        if session["finished"]:
            return True
        directory = self._directory()
        info = os.stat(key + ".jsonl", dir_fd=directory, follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            return False
        filename = key + ".summary.json"
        try:
            existing = os.stat(filename, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(existing.st_mode):
                return False
        temporary = key + "." + secrets.token_hex(8) + ".tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=directory)
        try:
            payload = (json.dumps(self._summary(session["turns"]), ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
            self._write(descriptor, payload)
            os.replace(temporary, filename, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
        session["finished"] = True
        session["turns"].clear()
        return True

    def close(self) -> bool:
        if not self.enabled or self._closed:
            return False
        with self._lock:
            success = True
            for key in self._sessions:
                try:
                    success = bool(self._finish(key)) and success
                except (OSError, TypeError, ValueError, UnicodeError):
                    success = False
            self._closed = True
            self._sessions.clear()
            self._key = None
            if self._directory_fd is not None:
                try:
                    os.close(self._directory_fd)
                except OSError:
                    success = False
                self._directory_fd = None
            return success
