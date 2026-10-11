from __future__ import annotations

import time
from dataclasses import asdict

from .classifier import MockIntentClassifier
from .content import ContentRepository
from .hardware import MockAudioOutput, MockHardwareAdapter, MockLEDController
from .profiles import MockProfileProvider
from .runtime import ScenarioRuntime
from .types import HardwareEvent, Session, SessionState


class MishkaMVPService:
    """Session boundary for mock HTTP/WebSocket and future hardware adapters."""
    def __init__(self, content_root=None, classifier=None, profiles=None, clock=time.monotonic, max_session_s: float = 20 * 60):
        self.clock, self.max_session_s = clock, max_session_s
        self.hardware = MockHardwareAdapter()
        self.led = MockLEDController(self.hardware)
        self.profiles = profiles or MockProfileProvider()
        self.audios: dict[str, MockAudioOutput] = {}
        self.sessions: dict[str, Session] = {}
        self.runtime = ScenarioRuntime(ContentRepository(content_root), classifier or MockIntentClassifier(), self.profiles, self.led, self.audio_for, clock)

    def audio_for(self, session_id: str) -> MockAudioOutput:
        return self.audios.setdefault(session_id, MockAudioOutput())

    def _session(self, session_id: str) -> Session:
        session = self.sessions.get(session_id)
        if not session:
            now = self.clock()
            session = Session(session_id=session_id, created_at=now, last_activity_at=now, node_entered_at=now)
            self.sessions[session_id] = session
        return session

    def process(self, session_id: str, payload: dict) -> dict:
        event = self.hardware.parse(payload)
        session = self._session(session_id)
        if session.active and self.clock() - session.created_at > self.max_session_s:
            self._cancel(session)
            outputs = self.runtime.goto(session, "end", "session_duration_limit")
            return self._result(session, outputs, "session_duration_limit")
        if event.type == "rfid":
            return self._rfid(session, event)
        if event.type == "handset":
            return self._handset(session, event)
        if event.type == "timeout":
            return self._result(session, self.runtime.timeout(session), "timeout_checked")
        if event.type == "button_hold" and (event.duration_ms or 0) >= 20_000:
            self._cancel(session)
            return self._result(session, self.runtime.goto(session, "end", "long_hold_exit"), "long_hold_exit")
        if event.type in {"button", "button_hold"}:
            # A physical click is delivered as pressed + released. The
            # release is telemetry only; routing it through the newly
            # selected node makes story/game choices fall into fallback.
            if event.type == "button" and event.state == "released":
                return self._result(session, [], "button_released")
            self._cancel(session)  # new child input has priority over pending audio
            return self._result(session, self.runtime.handle_button(session, event.button_id or 0), "button")
        if event.type == "text":
            self._cancel(session)
            return self._result(session, self.runtime.handle_text(session, event.text or ""), "text")
        return self._result(session, [], "ignored")

    def _rfid(self, session: Session, event: HardwareEvent) -> dict:
        self._cancel(session)
        profile = self.profiles.get_by_tag(event.tag_id or "")
        if profile:
            session.child_id, session.anonymous, session.state = profile.child_id, False, SessionState.AUTHENTICATED
            return self._result(session, [{"action": "auth", "status": "authenticated", "profile": profile.public()}], "rfid_known")
        session.child_id, session.anonymous, session.state = None, True, SessionState.WAITING_HANDSET
        return self._result(session, [{"action": "auth", "status": "anonymous", "reason": "unknown_or_missing_tag"}], "rfid_anonymous")

    def _handset(self, session: Session, event: HardwareEvent) -> dict:
        if event.state == "down":
            self._cancel(session)
            session.handset_up = False
            return self._result(session, self.runtime.goto(session, "end", "handset_down"), "handset_down")
        if event.state != "up":
            return self._result(session, [], "invalid_handset_state")
        self._cancel(session)
        session.handset_up = True
        if session.state == SessionState.WAITING_RFID:
            # Explicit anonymous mode when there is no RFID event.
            session.anonymous, session.state = True, SessionState.WAITING_HANDSET
        if session.state in {SessionState.AUTHENTICATED, SessionState.WAITING_HANDSET, SessionState.ENDING, SessionState.RESET}:
            outputs = self.runtime.start(session)
            return self._result(session, outputs, "handset_up")
        return self._result(session, [], "handset_ignored")

    def _cancel(self, session: Session) -> None:
        session.response_token += 1
        self.audio_for(session.session_id).cancel(session.response_token)

    def reset(self, session_id: str) -> dict:
        session = self.sessions.pop(session_id, None)
        audio = self.audios.pop(session_id, None)
        if audio:
            audio.clear()
        return {"session_id": session_id, "reset": bool(session), "state": SessionState.RESET.value}

    def snapshot(self, session_id: str) -> dict | None:
        session = self.sessions.get(session_id)
        if not session:
            return None
        data = asdict(session)
        data["state"] = session.state.value
        return data

    def _result(self, session: Session, outputs: list[dict], reason: str) -> dict:
        return {"session_id": session.session_id, "state": session.state.value, "node": session.current_node, "reason": reason, "outputs": outputs}
