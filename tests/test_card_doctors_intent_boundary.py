"""DATA14 doctor lists cannot answer exact route, time or appointment-order questions."""
import json

import pytest

from app.foundation.canonical_services import conversational_information_intent, day_information_intent
from app.foundation.domain import Event
from app.foundation.validation import AsyncResponseValidator, OllamaMiniLLMValidator
from test_card_doctors_typed import Harness, Model, NAME, doctor_card


class ForbiddenSchedule:
    def __init__(self):
        self.calls = 0

    async def schedule(self, *_args):
        self.calls += 1
        raise AssertionError("doctor-list facts cannot authorize schedule access")


async def ask_card_question(text, scene):
    # Use the existing actual mapper/profile/App helpers and mock speech adapters.
    names = [NAME, "PRIVATE SECOND SPECIALIST", "PRIVATE THIRD SPECIALIST"]
    h = Harness(doctor_card(names))
    spoken, semantic_requests = [], []
    provider, model = ForbiddenSchedule(), Model()
    mini = OllamaMiniLLMValidator()

    async def request(_payload):
        semantic_requests.append(True)
        return json.dumps({"message":{"content":json.dumps({"decision":"allow","confidence":1.})}}).encode()

    mini._request_bytes = request  # In-memory counter; never invokes a model/network.
    h.app.bounded_s2s.response_validator = AsyncResponseValidator(mini)
    h.app.bounded_s2s.llm = model
    h.app.schedule_weather = provider
    synthesize = h.app.bounded_s2s.tts.delegate.synthesize

    async def record(value, *args):
        spoken.append(value)
        return await synthesize(value, *args)

    h.app.bounded_s2s.tts.delegate.synthesize = record
    try:
        await h.ready()
        h.state.mode, h.state.handset = ("root_menu" if scene == "schedule_weather" else scene), "up"
        if scene == "schedule_weather":
            # Enter through the real menu edge so the descriptor is committed
            # before voice admission; never force a scene phase in this fixture.
            await h.send("ButtonPressed", {"button_id":7})
            await h.send("ButtonReleased", {"button_id":7})
            for _ in range(4):
                if h.state.scene_menu_phase == "ready":
                    break
                if h.state.scene_menu_phase == "repeat_listen":
                    h.sequence += 1
                    await h.app.receive_async(Event("SpeechReceived", h.boundary.session_id,
                        h.sequence, 1., "test", {}, f"menu-boundary:{h.sequence}", h.clock[0]), "Нет")
                else:
                    await h.complete()
            assert h.state.mode == "schedule_weather" and h.state.scene_menu_phase == "ready"
            assert h.state.scene_menu_descriptor["scene"] == "schedule_weather"
        assert provider.calls == 0
        # Public menu setup is outside the question's model/privacy receipt.
        spoken.clear(); semantic_requests.clear(); model.calls.clear(); h.played.clear()
        h.sequence += 1
        result = await h.app.receive_async(Event("SpeechReceived", h.boundary.session_id,
            h.sequence, 1., "test", {}, f"card-boundary:{h.sequence}", h.clock[0]), text)
        # These guards execute before the expected RED assertion, so every
        # negative case independently proves the no-provider/no-grant boundary.
        assert {"schedule_calls":provider.calls, "model_calls":len(model.calls),
                "semantic_calls":len(semantic_requests),
                "schedule_scope":h.app._identity_scope_live(h.boundary.session_id, "schedule"),
                "schedule_ref":h.app._canonical_schedule_refs[h.boundary.session_id]} == {
                "schedule_calls":0, "model_calls":0, "semantic_calls":0,
                "schedule_scope":False, "schedule_ref":None}
        public = json.dumps((result, h.app.audio.commands,
            h.app._canonical_audio_context(h.boundary.session_id).system_prompt), ensure_ascii=False)
        assert all(name not in public for name in names)
        return h, result, spoken, names
    except BaseException:
        await h.close()
        raise


@pytest.mark.asyncio
async def test_explicit_doctor_list_reply_is_source_qualified_without_a_plan():
    text = "Какие у меня сегодня врачи?"
    assert conversational_information_intent(text) == "card_doctors"
    h, result, spoken, names = await ask_card_question(text, "bear_dialogue")
    try:
        facts = h.app._canonical_profiles[h.boundary.session_id]
        assert facts["card_doctors_source"] == "data14.v1"
        assert facts["card_visit_date"] == "2029-01-01"
        assert len(facts["card_doctor_names"]) == 3
        assert h.state.identity_capabilities == ["personalize"]
        assert h.state.active_audio_id == "TYPED_CARD_DOCTORS"
        assert any(item.get("kind") == "card_doctors" and
                   item.get("resolution") == "typed_information_audio_queued"
                   for item in result["outputs"])
        assert len(spoken) == 1
        reply = spoken[0]
        # Check exact source-qualified template without printing private values.
        checks = [reply.startswith("В твоей карточке на 01.01.2029 указаны: "),
            names[0] in reply, names[1] in reply, names[2] not in reply,
            "Это часть списка." in reply,
            reply.endswith("Точного плана, времени и кабинета у меня нет."),
            "следующий" not in reply.casefold()]
        assert all(checks)
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("scene,text,expected_kind", [
    pytest.param("schedule_weather", "Куда мне сейчас идти?", "where_now", id="route-day"),
    pytest.param("bear_dialogue", "Куда мне сейчас идти?", "where_now", id="route-bear"),
    pytest.param("bear_dialogue", "Когда мой следующий приём?", "what_next", id="next-appointment"),
    pytest.param("schedule_weather", "Во сколько мой приём?", "what_next", id="appointment-time"),
    pytest.param("schedule_weather", "В какой кабинет мне идти?", "where_now", id="appointment-room"),
])
async def test_exact_plan_question_cannot_be_replaced_by_a_doctor_list(scene, text, expected_kind):
    classifier = day_information_intent if scene == "schedule_weather" else conversational_information_intent
    assert classifier(text) == expected_kind
    h, result, spoken, _names = await ask_card_question(text, scene)
    try:
        requests = [item for item in result["outputs"] if item.get("type") == "typed_info_request"]
        # A redacted enum/count receipt keeps failure logs free of source names.
        observed = {"kinds":[item.get("kind") for item in requests],
            "resolutions":[item.get("resolution") for item in requests],
            "private_card_audio":h.state.active_audio_id == "TYPED_CARD_DOCTORS",
            "private_tts_count":len(spoken),
            "truthful_fallback":"PHRASE_101" in h.played}
        assert observed == {"kinds":[expected_kind],
            "resolutions":["typed_information_blocked_by_identity"],
            "private_card_audio":False, "private_tts_count":0, "truthful_fallback":True}
    finally:
        await h.close()
