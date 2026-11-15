import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
import pytest
from app.foundation.personal_story import PersonalStoryService, DURATION_BOUNDS
from app.foundation.s2s import AudioChunk, S2SConfig
from app.foundation.validation import AsyncResponseValidator, ValidationDecision

TEXT='Зайчик нашёл семечко. Он захотел вырастить цветок. Земля была сухая. Зайчик принёс воду. Друзья помогли ему. Скоро появился росток. Потом распустился красивый цветок. Друзья вместе любовались цветком.'
class Mini:
    supports_full_story=True
    async def validate(self,text,context=()):
        return ValidationDecision('allow',True,'test_full_story',.99,'test')
class LLM:
    supports_full_story=True  # Explicit synthetic full-story provider, not production approval.
    def __init__(self,band='3-5',text=TEXT):self.band=band;self.text=text;self.calls=[]
    async def stream_story(self,messages,request_id,cancelled):
        self.calls.append(messages)
        parts=self.text.split('. '); plot=dict(zip(('setting','problem','attempt','resolution'), ['. '.join(parts[:2])+'.', '. '.join(parts[2:4])+'.', '. '.join(parts[4:6])+'.', '. '.join(parts[6:])]))
        yield json.dumps({'title':'Цветок','plot':plot,'age_band':self.band,'estimated_duration_sec':DURATION_BOUNDS[self.band][0], 'safety_tags':['safe'],'ending_state':'safe_resolved'},ensure_ascii=False)
class TTS:
    def __init__(self,duration):self.duration=duration;self.calls=[]
    async def synthesize(self,text,token,cancelled):
        self.calls.append(text)
        return AudioChunk((.01,)*int(self.duration*1000/8),1000,token,'')
class Voice:
    async def convert_private(self,audio,cancelled):return audio

def engine(band='3-5',duration=None,text=TEXT):
    return SimpleNamespace(llm=LLM(band,text),tts=TTS(duration or DURATION_BOUNDS[band][0]),voice=Voice(),
        response_validator=AsyncResponseValidator(mini=Mini()),config=S2SConfig())

@pytest.mark.asyncio
@pytest.mark.parametrize('band',list(DURATION_BOUNDS))
async def test_full_story_validated_then_measured_all_age_bands(band):
    e=engine(band);audio=await PersonalStoryService(e).render('лес',band,(),1,lambda:True)
    assert audio and len(audio.samples)/audio.sample_rate==DURATION_BOUNDS[band][0]
    assert '8 words' in e.llm.calls[0][0]['content'] if band=='1-3' else True

@pytest.mark.asyncio
@pytest.mark.parametrize('duration',[1,59,101,200])
async def test_metadata_does_not_replace_actual_pcm_duration(duration):
    assert await PersonalStoryService(engine(duration=duration)).render('лес','3-5',(),1,lambda:True) is None

@pytest.mark.asyncio
async def test_threat_entire_story_rejected_before_tts():
    e=engine(text=TEXT+' Принимай таблетки.')
    assert await PersonalStoryService(e).render('лес','3-5',(),1,lambda:True) is None
    assert not e.tts.calls

@pytest.mark.asyncio
async def test_private_story_never_uses_shared_tts_cache_or_plain_voice():
    from app.foundation.s2s import CachedTTSProvider, RetryingLLMProvider
    e=engine();e.llm=RetryingLLMProvider(e.llm)
    delegate=e.tts
    e.tts=CachedTTSProvider(delegate)
    async def forbidden(*args,**kwargs):raise AssertionError('shared cache used')
    e.tts.synthesize=forbidden
    async def plain(*args):raise AssertionError('non-private RVC used')
    e.voice.convert=plain
    assert await PersonalStoryService(e).render('лес','3-5',(),1,lambda:True)
    assert delegate.calls

@pytest.mark.asyncio
async def test_whole_text_semantic_failure_precedes_any_tts():
    e=engine()
    class Block:
        supports_full_story=True
        async def validate(self,text,context=()):return ValidationDecision('block',False,'semantic_test',.99,'test')
    e.response_validator=AsyncResponseValidator(mini=Block())
    assert await PersonalStoryService(e).render('лес','3-5',(),1,lambda:True) is None
    assert not e.tts.calls


@pytest.mark.asyncio
async def test_one_sentence_with_fake_duration_is_not_a_complete_story():
    e=engine()
    async def one(*args):
        yield json.dumps({'title':'История','text':'Зайчик вернулся домой.','age_band':'3-5','estimated_duration_sec':60,'safety_tags':['safe'],'ending_state':'safe_resolved'})
    e.llm.stream_story=one
    assert await PersonalStoryService(e).render('лес','3-5',(),1,lambda:True) is None
    assert not e.tts.calls

def test_default_mock_validator_does_not_advertise_full_story_capability():
    e=engine();e.response_validator=AsyncResponseValidator()
    assert not PersonalStoryService(e).capable()
    class Unspecified:
        async def validate(self,*args):return ValidationDecision('allow',True,'unspecified',.99,'test')
    e.response_validator=Unspecified()
    assert not PersonalStoryService(e).capable()

@pytest.mark.asyncio
async def test_duplicate_external_text_field_is_rejected():
    e=engine();original=e.llm.stream_story
    async def mismatch(*args):
        async for raw in original(*args):
            value=json.loads(raw);value['text']='Зайчик вернулся домой.'
            yield json.dumps(value)
    e.llm.stream_story=mismatch
    assert await PersonalStoryService(e).render('лес','3-5',(),1,lambda:True) is None
    assert not e.tts.calls

@pytest.mark.asyncio
async def test_aggregate_pcm_ceiling_stops_synthesis_before_joining_oversize_story():
    e=engine(duration=160)
    assert await PersonalStoryService(e).render('лес','3-5',(),1,lambda:True) is None
    assert len(e.tts.calls)==6  # 6*20s exceeds accepted100s, not all8sentences.
