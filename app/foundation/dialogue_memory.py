"""RAM-only committed dialogue, independent of in-flight speech generations."""
from __future__ import annotations

import asyncio
import copy


class DialogueMemoryMixin:
    def _drop_dialogue_context(self, session_id: str) -> None:
        self.conversation_contexts.pop(session_id, None)
        suspended = getattr(self, "_suspended_dialogue_contexts", None)
        if isinstance(suspended, dict):
            suspended.pop(session_id, None)
        task = self._dialogue_expiry_tasks.pop(session_id, None)
        if task is not None and not task.done():
            try:
                if task is not asyncio.current_task():
                    task.cancel()
            except RuntimeError:
                pass

    def expire_dialogue_memory(self) -> None:
        """Also run on ingress: a delayed timer never extends read authority."""
        now = self.monotonic_now()
        for sid, context in tuple(self.conversation_contexts.items()):
            deadline = getattr(context, "_ram_expires_at", None)
            if deadline is not None and now >= deadline:
                self._drop_dialogue_context(sid)

    def _schedule_dialogue_expiry(self, session_id: str, context) -> None:
        old = self._dialogue_expiry_tasks.pop(session_id, None)
        if old is not None:
            old.cancel()
        if not self.background_scheduler:
            return
        deadline = context._ram_expires_at

        async def expire() -> None:
            try:
                while self.monotonic_now() < deadline:
                    await asyncio.sleep(max(0.001, deadline - self.monotonic_now()))
                current = self.conversation_contexts.get(session_id)
                if current is not None and getattr(current, "_ram_expires_at", None) == deadline:
                    self._drop_dialogue_context(session_id)
            except asyncio.CancelledError:
                return
            finally:
                if self._dialogue_expiry_tasks.get(session_id) is asyncio.current_task():
                    self._dialogue_expiry_tasks.pop(session_id, None)

        self._dialogue_expiry_tasks[session_id] = asyncio.create_task(
            expire(), name="mishka-dialogue-ram-expiry-" + session_id
        )

    def _transition_dialogue_memory(self, session_id, event_type, previous_state,
                                    state, *, takes_ownership, exhibition_reset):
        self.expire_dialogue_memory()
        caller = self._game_callers.get(session_id)
        owner = (self._session_epochs.get(session_id, 0),
                 getattr(state, "profile_ref", None), getattr(state, "identity_request_id", None))
        if ((takes_ownership and (exhibition_reset or event_type in {"HandsetPlaced", "AnonymousStart", "SessionCancel", "SessionReset",
                "HardwareDisconnected", "InteractionIdleExpired"})) or state is None
                or state.safety_severity != "none" or (caller and caller["owner"] != owner)):
            self._game_callers.pop(session_id, None)
        elif (takes_ownership and previous_state is not None and previous_state.mode == "bear_dialogue"
                and state.mode != "bear_dialogue" and (state.game_return_mode == "bear_dialogue"
                    or state.activity_return_mode == "bear_dialogue")):
            context = self.conversation_contexts.pop(session_id, None)
            if context is not None:
                self._game_callers[session_id] = {"context": copy.deepcopy(context), "owner": owner,
                    "suspended_at": self.monotonic_now()}
        elif (takes_ownership and caller and previous_state is not None
                and state.mode not in {"game_menu", "game_active", "fixed_story_menu", "fixed_story_playing",
                    "story_speech_analysis", "story_pause_announcement", "story_paused", "story_resume_announcement", "fixed_story_choice_wave"}):
            self._game_callers.pop(session_id, None)
            if state.mode == "bear_dialogue" and (previous_state.game_return_mode == "bear_dialogue"
                    or previous_state.activity_return_mode == "bear_dialogue"):
                context = caller["context"]
                context.started_at += max(0., self.monotonic_now() - caller["suspended_at"])
                self.conversation_contexts[session_id] = context
        current = self.conversation_contexts.get(session_id)
        if current is None or not takes_ownership:
            return
        identity = state.profile_ref if state is not None else None
        same_identity = getattr(current, "_owner", (None, None))[1] == identity
        authorization_live = not (
            self.optional_identity and state is not None and state.identity_request_id
            and not self._identity_scope_live(session_id, "personalize")
        )
        if (exhibition_reset or event_type in {"AnonymousStart", "SessionCancel", "SessionReset", "HardwareDisconnected"}
                or not same_identity or not authorization_live or state is None
                or state.dialogue_memory.status == "expired"):
            self._drop_dialogue_context(session_id)
            return
        soft_idle = event_type == "InteractionIdleExpired"
        if soft_idle and self.canonical_runtime.exhibition_mode:
            self._drop_dialogue_context(session_id)
            return
        clinical_down = event_type in {"HandsetPlaced", "InteractionIdleExpired"} and not self.canonical_runtime.exhibition_mode
        if clinical_down:
            # Keep only completed turns. Providers work on detached copies and
            # cannot mutate this snapshot after hangup, replacement or reset.
            if not current.history:
                self._drop_dialogue_context(session_id)
                return
            current = copy.deepcopy(current)
            current._owner = (self._session_epochs.get(session_id, 0), identity)
            current._ram_suspended_at = self.monotonic_now()
            current._ram_expires_at = (getattr(current, "_ram_expires_at", None)
                if soft_idle and getattr(current, "_ram_expires_at", None) is not None
                else self.monotonic_now() + state.dialogue_memory.ttl_seconds)
            self.conversation_contexts[session_id] = current
            self._schedule_dialogue_expiry(session_id, current)
        elif (previous_state is not None and previous_state.mode in {"bear_dialogue", "support_intake"}
              and state.mode != previous_state.mode):
            self._drop_dialogue_context(session_id)
        elif getattr(current, "_ram_expires_at", None) is not None and event_type in {"HandsetLifted", "ButtonPressed", "VoiceIntentReady"}:
            # Real interaction resets the inactivity clock; audio callbacks,
            # key releases and watchdog ticks cannot keep private RAM alive.
            current._ram_expires_at = self.monotonic_now() + state.dialogue_memory.ttl_seconds
            # Keep canonical suspension metadata on the same real-activity
            # clock; otherwise the next event could expire it prematurely.
            state.dialogue_memory.touch(self.now())
            state.timeout_at = state.dialogue_memory.expires_at
            self._schedule_dialogue_expiry(session_id, current)

    def _resume_dialogue_context(self, session_id: str, context) -> None:
        suspended = getattr(context, "_ram_suspended_at", None)
        if suspended is None:
            return
        # Retain the active conversational budget, not time spent on hook.
        context.started_at += max(0.0, self.monotonic_now() - suspended)
        del context._ram_suspended_at
        del context._ram_expires_at
        task = self._dialogue_expiry_tasks.pop(session_id, None)
        if task is not None:
            task.cancel()
