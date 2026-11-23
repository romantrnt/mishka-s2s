"""Synthetic profiles/audio only: no captured visitor data or external providers."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import wave

import pytest
from app.foundation.canonical_contract import CanonicalRuntime, CanonicalState
from app.foundation.identity import normalize_profile
from app.foundation.name_audio import NameAudioRegistry, render_phrase_with_name
from app.foundation.s2s import AudioChunk, ProfileContext
from app.foundation.visit_profile import VisitProfile, infer_grammatical_gender


@pytest.mark.parametrize('name,gender', [('Александр','masculine'),('Александра','feminine'),('Ваня','masculine'),('Маша','feminine'),('Саша','neutral'),('Женя','neutral'),('Валя','neutral'),('Неизвестное','neutral'),('Александр скажи','neutral')])
def test_unambiguous_names_only(name, gender):
    assert infer_grammatical_gender(name) == gender


@pytest.mark.parametrize('explicit', ['feminine','masculine','neutral'])
def test_explicit_gender_survives_all_profile_views(explicit):
    profile = normalize_profile({'profile_ref':'synthetic', 'display_name':'Александр', 'grammatical_gender':explicit})
    state = CanonicalRuntime()._new_state('synthetic-visit',profile)
    assert state.grammatical_gender == explicit
    assert state.visit_profile(profile).grammatical_gender == explicit
    assert ProfileContext.from_profile(profile).grammatical_gender == explicit


def test_uncertain_name_and_private_interests_are_not_reinferred_or_projected():
    assert infer_grammatical_gender('Маша',name_confident=False) == 'neutral'
    state = CanonicalState('synthetic-visit', display_name='Маша', grammatical_gender='neutral', interests=['cars'])
    assert state.visit_profile().grammatical_gender == 'neutral'
    assert 'display_name' not in state.snapshot() and 'interests' not in state.snapshot()


def pack(tmp_path):
    path = tmp_path/'name.wav'
    with wave.open(str(path),'wb') as w:
        w.setparams((1,2,24000,0,'NONE','not compressed'))
        w.writeframes(b'\x00\x40'*2400)
    manifest = tmp_path/'manifest.json'
    manifest.write_text(json.dumps({'entries':[{'key':'Саша','file':path.name,'case':'nominative','sha256':hashlib.sha256(path.read_bytes()).hexdigest()}]}))
    return NameAudioRegistry(manifest)


@pytest.mark.asyncio
async def test_recording_is_inserted_once_inside_generated_phrase(tmp_path):
    registry=pack(tmp_path); calls=[]
    async def synth(text):
        calls.append(text)
        return AudioChunk((.1,)*2400,24000,7)
    audio,reason=await render_phrase_with_name('Однажды {name} нашёл карту.','Саша',registry=registry,synthesize_fn=synth)
    assert reason=='recorded_name_spliced'
    assert calls==['Однажды','нашёл карту.']
    assert audio.token==7 and len(audio.samples)==3*2400+2*840
    assert registry.validate().ok


@pytest.mark.asyncio
async def test_missing_recording_uses_one_whole_phrase_and_wrong_case_is_neutral(tmp_path):
    calls=[]
    async def synth(text):
        calls.append(text);return AudioChunk((.1,)*24,24000,3)
    registry=pack(tmp_path)
    _,reason=await render_phrase_with_name('Привет, {name}!','Маша',registry=registry,synthesize_fn=synth)
    assert reason=='tts_full_phrase' and calls==['Привет, Маша!']
    calls.clear()
    _,reason=await render_phrase_with_name('Подарок для {name}.','Саша',registry=registry,case='genitive',synthesize_fn=synth,neutral_fallback='Для тебя подарок.')
    assert reason=='neutral_fallback' and calls==['Для тебя подарок.']
    audio,reason=await render_phrase_with_name('{name} и {name}.','Саша',registry=registry,synthesize_fn=synth)
    assert audio is None and reason=='invalid_name_slots'


def test_bad_manifest_and_escaping_path_do_not_break_startup(tmp_path):
    manifest=tmp_path/'manifest.json';manifest.write_text('{bad')
    assert NameAudioRegistry(manifest).resolve('Саша') is None
    manifest.write_text(json.dumps({'entries':[{'key':'Саша','case':'nominative','file':'../outside.wav'}]}))
    assert not NameAudioRegistry(manifest).entries

@pytest.mark.asyncio
async def test_max_ready_copies_interests_and_trigger_constraints_without_changing_max():
    from test_profile_onboarding import Harness,card
    value=card('Маша');value['identity']['grammatical_gender']='neutral'
    value['interests']=['машины']
    value['adaptation']['trigger_topics']=[{'topic_id':'animal_fear','status':'active','handling':'adult_neutral'}]
    h=Harness(value)
    try:
        await h.ready()
        assert h.state.interests==['cars'] and h.state.grammatical_gender=='neutral'
        assert 'animal_fear' in h.state.visit_profile().trigger_topics
        h.state.mode='menu_ready';h.state.silent_mode=True;h.state.interaction_route='panel'
        result=await h.send('ButtonPressed',{'button_id':3})
        assert h.state.mode=='personal_story_theme',result
        assert len(h.state.scene_menu_descriptor['items'])==5
        assert all(not set(item['tags']) & {'animals','dinosaurs','godzilla'} for item in h.state.personal_story_selection)
        assert value['identity']['grammatical_gender']=='neutral'
        assert result['state']=='personal_story_theme'
    finally:await h.close()


@pytest.mark.asyncio
async def test_guest_neutral_survives_full_voice_prompt_context():
    from test_scene_menu_runtime import Harness
    h=Harness()
    try:
        h.state.display_name='Маша';h.state.grammatical_gender='neutral';h.state.onboarding_name_collected=True
        context=h.app._canonical_audio_context(h.sid)
        assert context.profile.display_name=='Маша' and context.profile.grammatical_gender=='neutral'
    finally:await h.close()


@pytest.mark.asyncio
async def test_private_tts_rejects_wrong_tokens_and_capability_requires_voice():
    from app.foundation.name_audio import synthesize_private_text
    from app.foundation.demo_story_audio import capable
    class Wrong:
        async def synthesize(self,text,token,cancelled):return AudioChunk((.1,)*24,24000,token-1)
    assert await synthesize_private_text(Wrong(),'Тест.',4,lambda:False) is None
    assert not capable(SimpleNamespace(tts=Wrong(),response_validator=SimpleNamespace(validate_trusted=lambda text:None)))


@pytest.mark.asyncio
async def test_native_missing_recording_keeps_first_chunk_streaming(tmp_path):
    import asyncio
    from app.foundation.name_audio import VisitNameTTSProvider
    release=asyncio.Event()
    class Native:
        provider_name='vox555'
        async def synthesize_audio_stream(self,text,token,cancelled,*,first_chunk=True):
            yield AudioChunk((.1,)*24,24000,token)
            await release.wait()
            yield AudioChunk((.2,)*24,24000,token)
    adapter=VisitNameTTSProvider(Native(),'Маша',pack(tmp_path))
    stream=adapter.synthesize_audio_stream('Привет, Маша!',9,lambda:False)
    try:
        first=await asyncio.wait_for(anext(stream),.3)
        assert first.token==9 and not release.is_set()
    finally:
        release.set();await stream.aclose()


@pytest.mark.asyncio
async def test_generated_reply_uses_recording_once_and_avoids_punctuation_tts(tmp_path):
    from app.foundation.name_audio import VisitNameTTSProvider
    from app.foundation.s2s import _native_audio_stream_method
    calls=[]
    class TTS:
        async def synthesize(self,text,token,cancelled):
            assert any(c.isalnum() for c in text)
            calls.append(text);return AudioChunk((.1,)*240,24000,token)
    adapter=VisitNameTTSProvider(TTS(),'Саша',pack(tmp_path))
    assert _native_audio_stream_method(adapter) is None
    audio=await adapter.synthesize('Ого, Саша!',8,lambda:False)
    assert calls==['Ого,'] and audio.token==8


@pytest.mark.parametrize('optional',[
    {'interests':['cars\x00']},
    {'sensory_flags':['quiet\x00']},
    {'trigger_topics':[{'topic_id':[],'status':'active'}]},
    {'trigger_topics':[{'topic_id':'animal_fear','status':'active','handling':[]}]},
])
def test_bad_optional_max_values_preserve_basic_valid_identity(optional):
    from datetime import datetime,timezone
    from test_profile_onboarding import card
    from app.foundation.max_json import _map_card
    value=card('Синтетик');value.update(optional)
    profile,deadline,facts=_map_card(value,'synthetic',datetime(2029,1,1,tzinfo=timezone.utc),10.)
    assert profile['display_name']=='Синтетик' and deadline>10.


@pytest.mark.asyncio
async def test_full_conversation_queue_uses_recorded_name_in_validated_reply(tmp_path):
    from app.foundation.config import Settings
    from app.foundation.s2s import BoundedConversationEngine,ConversationContext,MockLLMProvider,MockPlaybackController,PassthroughVoiceProvider
    calls=[]
    class TTS:
        async def synthesize(self,text,token,cancelled):
            calls.append(text);return AudioChunk((.1,)*240,24000,token)
    engine=BoundedConversationEngine(MockLLMProvider(tokens=['<say>Ого, Саша!</say>']),TTS(),PassthroughVoiceProvider(),MockPlaybackController(),settings=Settings(profile='mock',voice_conversion='off'),allow_quick_replies=False)
    engine.name_audio_registry=pack(tmp_path)
    context=ConversationContext('synthetic-queue','canonical_product','bear_dialogue',('__all__',),('diagnosis','medicine'),2,120,2)
    context.profile=ProfileContext(display_name='Саша',grammatical_gender='neutral')
    result=await engine.run_text_turn('Давай ещё',context)
    assert result.fallback_reason is None,result
    assert calls==['Ого,']


def test_root_menu_recording_receipt_matches_catalog_and_digit_timings():
    from app.foundation.application import EXHIBITION_MENU_PHASE_TIMINGS_MS
    from app.foundation.voice_catalog import load_phrase_catalog
    manifest=json.loads(Path('content/audio/voice_manifest.json').read_text())
    entry=next(e for e in manifest['assets'] if e['phrase_id']=='PHRASE_008')
    phrases=load_phrase_catalog('content/audio/phrase_catalog_ru.txt')
    assert 'Три — новая сказка. Четыре — рассказать или спросить.' in phrases['PHRASE_008']==entry['transcript']
    assert entry['content_hash']==hashlib.sha256(entry['transcript'].encode()).hexdigest()
    assert entry['asset_sha256']==hashlib.sha256(Path('content/audio/voice/PHRASE_008.wav').read_bytes()).hexdigest()
    assert (4,'digit',7670) in EXHIBITION_MENU_PHASE_TIMINGS_MS
    assert (4,'full',8700) in EXHIBITION_MENU_PHASE_TIMINGS_MS
