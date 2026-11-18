"""Private in-process consumer of Max's public PhoneUbuntuClient interface.

Max alone owns capture, UID, HTTPS, certificates, lookup and DB access. MISHKA
only receives patient_ready/clear and the already validated JSON card. This
module contains no reader, HTTP client, SQL access or substitute UID resolver.
"""
from __future__ import annotations

import asyncio
import math
import uuid
from datetime import datetime, timezone

from .identity import IdentityValidationError, normalize_profile, _bounded_text, _card_doctor_facts


def _map_card(card, context_ref, now, monotonic_now):
    if not isinstance(card, dict) or card.get("schema_version") != 1:
        raise ValueError("invalid_client_card")
    if card["record"]["state"] != "active" or card["rfid"]["state"] != "active" or card["visit"]["status"] != "open":
        raise ValueError("inactive_client_card")
    until = datetime.fromisoformat(card["rfid"]["valid_until"].replace("Z", "+00:00"))
    if until.utcoffset() is None:
        raise ValueError("invalid_client_expiry")
    remaining = (until - now).total_seconds()
    if not math.isfinite(remaining) or remaining <= 0:
        raise ValueError("expired_client_card")
    profile = {"profile_ref": context_ref}
    first_name = card["identity"]["first_name"]
    if first_name is not None:
        # DATA14 accepts 160 characters; the speech profile accepts 80.
        # Keep the valid card without a spoken name rather than truncate it.
        _bounded_text(first_name, "first_name", 160)
        if len(first_name) <= 80:
            profile["display_name"] = first_name
    birth = card["birth"]
    if birth["age_years"] is not None:
        profile.update(age_years=birth["age_years"], age_source="data14_age_years",
                       birth_data_state=birth["birth_data_state"])
    doctors, visit = card.get("doctors_today"), card["visit"]
    try:
        if (not isinstance(doctors, list) or len(doctors) > 32
                or visit.get("rfid_state") != "active" or visit.get("doctors_today") != doctors):
            raise IdentityValidationError("invalid DATA-14 doctor list")
        for row in doctors:
            if not isinstance(row, dict) or set(row) != {"code", "display_name"}:
                raise IdentityValidationError("invalid DATA-14 doctor row")
            _bounded_text(row["code"], "doctor_code", 160)
        profile.update(_card_doctor_facts({
            "card_doctor_names": [row["display_name"] for row in doctors],
            "card_visit_date": visit.get("date"), "card_doctors_source": "data14.v1",
        }))
    except IdentityValidationError:
        # Optional clinical facts can be unavailable without losing basic identity.
        pass
    # These are facts for future local scenario use, not prompts/flags/schedules.
    kind = card["acquaintance"]["kind"]
    if kind not in {"first", "repeat"}:
        raise ValueError("invalid_acquaintance")
    adaptation = card["adaptation"]
    if type(adaptation["wheelchair_access"]) is not bool or adaptation["participation_mode"] not in {"seated", "standing"}:
        raise ValueError("invalid_adaptation")
    facts = {"acquaintance": kind, "wheelchair_access": adaptation["wheelchair_access"],
             "participation_mode": adaptation["participation_mode"]}
    return normalize_profile(profile), monotonic_now + remaining, facts


class MaxJsonBoundary:
    """One serialized event-loop owner for poll/patient/clear/close.

    The client enforces its existing 120s deadline through poll(). We do not
    restart that deadline at dequeue. MISHKA independently enforces server
    expiry and its ready-to-lift deadline. Pump failure clears private context.
    """
    def __init__(self, application, client, *, session_id="max-json", now=None, admits_users=None):
        if not application.max_json:
            raise ValueError("max_json_mode_required")
        if application._max_client_boundary is not None:
            raise ValueError("client_owner_already_attached")
        self.app, self.client, self.session_id = application, client, session_id
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.admits_users = admits_users
        self.generation = 0
        self._context_namespace = uuid.uuid4().hex
        self._started = False
        self._closed = False
        self._close_succeeded = False
        self._healthy = False
        self._reader_ready = False
        self._stepping = False
        self._task = None
        self.facts = None
        application._max_client_boundary = self

    def clear_client(self):
        self.generation += 1
        self.facts = None
        self.client.clear()

    def start(self, *, background=True):
        if self._closed:
            raise RuntimeError("boundary_closed")
        if self._started:
            return False
        started = self.client.start()
        if not started:
            raise RuntimeError("client_already_owned_or_running")
        self._started = True
        self._healthy = False
        if background:
            self._task = asyncio.create_task(self.run(), name="mishka-max-json")
        return True

    async def _clear_context(self, *, expected_request_id=None, reason_code=None):
        state = self.app.canonical_runtime.sessions.get(self.session_id)
        if (expected_request_id is not None and state is not None
                and state.identity_request_id != expected_request_id):
            return {"reason": "newer_profile_owner_preserved"}
        self.facts = None
        if state is not None and not state.profile_ref and reason_code == "session_expired":
            return {"reason": "profile_already_clear"}
        if state is None or (not state.profile_ref and not state.active_story_id):
            return {"reason": "profile_already_clear"}
        event = self.app._identity_event(self.session_id, "ClientProfileCleared", {
            "reason_code": reason_code, "request_id": state.identity_request_id,
        })
        return await self.app._receive_canonical_event(event, identity_event_authorized=True)

    async def step(self):
        if self._closed:
            return None
        if self._stepping:
            raise RuntimeError("concurrent_client_poll_forbidden")
        self._stepping = True
        try:
            # Follow Max's public ordering: poll first, patient only on ready.
            event = self.client.poll()
            if event is None:
                return None
            if event.get("kind") == "reader_state":
                self._reader_ready = event.get("state") in {"connected", "ready"}
            if event.get("kind") == "clear":
                reason = event.get("reason_code")
                if reason not in {"scan_started", "session_expired", "no_personalization"}:
                    self._reader_ready = False
                if reason == "no_personalization":
                    self._healthy = True
                elif reason not in {"scan_started", "session_expired"}:
                    self._healthy = False
                self.generation += 1
                return await self._clear_context(reason_code=reason)
            if event.get("kind") != "patient_ready":
                if event.get("kind") == "reader_state" and event.get("state") in {"connected", "ready"}:
                    self._healthy = True
                return {"reason": "client_metadata"}
            if self.admits_users is not None and not self.admits_users():
                self.clear_client()
                await self._clear_context()
                return {"reason": "runtime_unavailable"}
            # A fresh validated card proves capture and API completed, but never
            # changes the explicitly synthetic data-source designation.
            self._reader_ready = True
            prior = self.app.canonical_runtime.sessions.get(self.session_id)
            prior_request = prior.identity_request_id if prior is not None else None
            # A bounded source queue may drop scan_started before ready. Each
            # ready still denotes a new local context, never a patient identity.
            self.generation += 1
            card = self.client.patient()
            try:
                generation = self.generation
                profile, expiry, facts = _map_card(card,
                    f"max-context:{self._context_namespace}:{generation}",
                    self.now(), self.app.monotonic_now())
            except (KeyError, TypeError, ValueError, OverflowError):
                self._healthy = False
                self.clear_client()
                await self._clear_context()
                return {"reason": "invalid_client_profile"}
            finally:
                card = None
            payload = {"request_id": uuid.uuid4().hex,
                       "client_generation": generation,
                       "expires_monotonic": expiry,
                       "ready_deadline": min(expiry, self.app.monotonic_now() + self.app.settings.profile_ready_ttl_s)}
            result = await self.app._receive_canonical_event(
                self.app._identity_event(self.session_id, "ClientProfileReady", payload),
                identity_event_authorized=True, identity_profile=profile)
            # The application may yield after committing the card. A valid
            # handset lift can then own a newer scenario revision while the
            # exact same profile request is still live. The return reason of
            # the earlier transition is not the profile-ownership authority.
            current = self.app.canonical_runtime.sessions.get(self.session_id)
            request_matches = bool(current is not None
                and current.identity_request_id == payload["request_id"]
                and current.profile_ref == profile["profile_ref"])
            if generation == self.generation:
                if request_matches and self.app._identity_scope_live(self.session_id, "personalize"):
                    self._healthy = True
                    self.facts = facts
                elif (current is None or not current.profile_ref or request_matches
                      or current.identity_request_id == prior_request):
                    # A rejected new card cannot coexist with the prior card.
                    # Preserve only a genuinely later request, not the owner
                    # that was already present before this ready was handled.
                    revoke_request = current.identity_request_id if current is not None else payload["request_id"]
                    self.clear_client()
                    await self._clear_context(expected_request_id=revoke_request)
            return result
        except asyncio.CancelledError:
            raise
        except Exception:
            self._reader_ready = False
            self._healthy = False
            self.generation += 1
            try:
                self.clear_client()
            except Exception:
                pass
            await self._clear_context()
            return {"reason": "client_unavailable"}
        finally:
            self._stepping = False

    async def run(self):
        try:
            while not self._closed:
                await self.step()
                await asyncio.sleep(0.05)
        finally:
            self._healthy = False
            try:
                self.clear_client()
                await self._clear_context()
            finally:
                if not self._closed:
                    await self.close()

    async def close(self):
        if self._closed:
            if not self._close_succeeded:
                raise RuntimeError("max_client_shutdown_incomplete")
            return True
        self._closed = True
        self._reader_ready = False
        self._healthy = False
        task, self._task = self._task, None
        pump_error = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as error:
                pump_error = error
        try:
            self.clear_client()
            await self._clear_context()
        finally:
            # Pump is stopped first; close no longer races poll/patient/clear.
            stopped = await asyncio.to_thread(self.client.close)
            self._close_succeeded = stopped is True
            if self._close_succeeded:
                self.app._max_client_boundary = None
        if not self._close_succeeded:
            raise RuntimeError("max_client_shutdown_incomplete")
        if pump_error is not None:
            raise RuntimeError("max_client_pump_cleanup_failed") from pump_error
        return True
