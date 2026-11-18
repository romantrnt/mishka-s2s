"""Measured drafting volume precedes whole-story safety and actual PCM bounds."""
import asyncio
import json
import re

import pytest

from app.foundation.personal_story import PersonalStoryService, DURATION_BOUNDS
from app.foundation.s2s import AudioChunk
from test_personal_story_service import engine, LLM, PLOT_SENTENCES, story_text


SHORT_PLOT=(
    'Зайчик нашёл маленькое семечко возле знакомой лесной тропинки. '
    'Он решил посадить его на солнечной поляне рядом с домиком. '
    'Белочка предложила помочь ему вырастить красивый цветок.',
    'Но земля оказалась сухой и совсем не хотела рассыпаться. '
    'Друзья поняли, что сначала нужно принести немного воды. '
    'Большая лейка осталась далеко, а корзинка пропускала воду.',
    'Белочка свернула широкий листочек в небольшую зелёную чашечку. '
    'Зайчик принёс воду из ручья и осторожно полил грядку. '
    'Потом друзья посадили семечко и каждый день проверяли росток.',
    'Скоро на поляне раскрылся чудесный золотистый лесной цветок. '
    'Зайчик пригласил белочку посмотреть на яркие лепестки. '
    'Она нарисовала цветок на листе и подарила рисунок другу. '
    'Теперь у них осталась память об общем добром деле и о своей весёлой маленькой общей победе.',
)


def wire(parts,band='4-5'):
    return json.dumps({'title':'Цветок','plot':dict(zip(('setting','problem','attempt','resolution'),parts)),
        'age_band':band,'estimated_duration_sec':sum(DURATION_BOUNDS[band])//2,
        'safety_tags':['safe'],'ending_state':'safe_resolved'},ensure_ascii=False)


@pytest.mark.asyncio
async def test_short_794_character_draft_gets_one_measured_repair_before_any_semantic_or_tts():
    text=' '.join(SHORT_PLOT)
    assert len(text)==794 and len(re.findall(r'[.!?]+',text))==13
    e=engine('4-5');events=[];original=e.llm.stream_story
    synthesize=e.tts.synthesize;short_samples=0;short_chunks=0
    short_sentences={s.strip() for s in re.findall(r'[^.!?]+[.!?]+',text)}
    async def measured(sentence,token,cancelled):
        nonlocal short_samples,short_chunks
        if sentence in short_sentences:
            samples,remainder=divmod(2757000,13)
            short_chunks+=1;samples+=short_chunks<=remainder;short_samples+=samples
            return AudioChunk((.01,)*samples,48000,token,'')
        return await synthesize(sentence,token,cancelled)
    e.tts.synthesize=measured
    async def generate(messages,request_id,cancelled):
        events.append('generation')
        if events.count('generation')==1:
            yield wire(SHORT_PLOT)
        else:
            assert not e.tts.calls
            assert len(messages)>1 and 'draft' in messages[-1]['content']
            async for value in original(messages,request_id,cancelled):yield value
    validate=e.response_validator.validate_story
    async def semantic(*args):
        events.append('semantic')
        return await validate(*args)
    e.llm.stream_story=generate;e.response_validator.validate_story=semantic
    audio=await PersonalStoryService(e).render('лес','4-5',(),1,lambda:True)
    assert audio and audio.duration_ms==60000
    assert events==['generation','generation','semantic']
    assert short_samples==0, 'the known57.4375s draft never reaches expensive synthesis'
    assert ' '.join(e.tts.calls)==story_text('4-5')


@pytest.mark.asyncio
@pytest.mark.parametrize('band',DURATION_BOUNDS)
async def test_one_repair_keeps_age_and_develops_all_four_parts(band):
    e=engine(band);messages_seen=[];original=e.llm.stream_story
    short=[' '.join(part[:2]) for part in PLOT_SENTENCES]
    async def generate(messages,request_id,cancelled):
        messages_seen.append(messages)
        if len(messages_seen)==1:yield wire(short,band)
        else:
            feedback=json.loads(messages[-1]['content'])
            minimum=7*sum(DURATION_BOUNDS[band])
            assert feedback['measured_russian_characters']<feedback['required_minimum']==minimum
            assert feedback['desired_volume']==[minimum,minimum+minimum//8]
            assert feedback['draft']['age_band']==band and 'text' not in feedback['draft']
            assert set(feedback['draft']['plot'])=={'setting','problem','attempt','resolution'}
            async for value in original(messages,request_id,cancelled):yield value
    e.llm.stream_story=generate
    audio=await PersonalStoryService(e).render('лес',band,(),1,lambda:True)
    assert audio and audio.duration_ms==DURATION_BOUNDS[band][0]*1000
    assert len(messages_seen)==2 and messages_seen[0][0]==messages_seen[1][0]


@pytest.mark.asyncio
@pytest.mark.parametrize('unbalanced',[False,True])
async def test_second_small_or_unbalanced_draft_fails_without_semantic_tts_or_private_logs(caplog,unbalanced):
    e=engine();calls=[];semantic=[]
    parts=[' '.join(part) for part in PLOT_SENTENCES] if unbalanced else list(SHORT_PLOT)
    if unbalanced:parts[0]='Зайчик пришёл.'
    def semantic_forbidden(*args):semantic.append(True);pytest.fail('undersized draft reached semantics')
    async def generate(*args):calls.append(True);yield wire(parts,'3-5')
    e.llm.stream_story=generate;e.response_validator.validate_story=semantic_forbidden
    assert await PersonalStoryService(e).render('лес','3-5',(),999,lambda:True) is None
    assert len(calls)==2 and not e.tts.calls and not semantic
    logs=[json.loads(r.getMessage().removeprefix('personal_story ')) for r in caplog.records
          if r.name=='mishka.personal_story']
    assert logs[-1]['reason']=='volume_rejected'
    assert logs[-1]['short_paragraphs']==(1 if unbalanced else 0)
    assert parts[-1] not in caplog.text and 'draft' not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid',['schema','unsafe','long_sentence'])
async def test_invalid_first_draft_is_not_repaired(invalid):
    e=engine();calls=[];candidate=json.loads(wire(SHORT_PLOT,'3-5'))
    if invalid=='schema':del candidate['title']
    if invalid=='unsafe':candidate['plot']['attempt']='Принимай таблетки.'
    if invalid=='long_sentence':candidate['plot']['attempt']=' '.join(['Слово']*19)+'.'
    async def generate(*args):calls.append(True);yield json.dumps(candidate,ensure_ascii=False)
    e.llm.stream_story=generate
    assert await PersonalStoryService(e).render('лес','3-5',(),1,lambda:True) is None
    assert len(calls)==1 and not e.tts.calls


@pytest.mark.asyncio
async def test_sufficient_text_does_not_override_exact_574375ms_pcm_rejection(caplog):
    e=engine('4-5');count=0;sentences=len(re.findall(r'[.!?]+',e.llm.text))
    async def synthesize(text,token,cancelled):
        nonlocal count
        count+=1;size,remainder=divmod(2757000,sentences)
        return AudioChunk((.01,)*(size+(count<=remainder)),48000,token,'')
    e.tts.synthesize=synthesize
    assert await PersonalStoryService(e).render('лес','4-5',(),1,lambda:True) is None
    assert len(e.llm.calls)==1 and count==sentences
    last=json.loads(caplog.records[-1].getMessage().removeprefix('personal_story '))
    assert last['reason']=='duration_rejected' and last['samples']==2757000 and last['rate']==48000


@pytest.mark.asyncio
async def test_repair_still_uses_actual_provider_context_budget_before_second_model_request():
    from app.foundation.s2s import OllamaLLMProvider
    from app.foundation.story_context import StoryContextError
    from test_story_backend_context import URL, MODEL, contract
    c=await contract();e=engine();llm=OllamaLLMProvider(URL,MODEL,1,num_ctx=16384,story_backend_contract=c)
    sent=[];candidate=json.loads(wire(SHORT_PLOT,'3-5'));candidate['title']='А'*6000
    async def generate(*args,**kwargs):sent.append(True);yield json.dumps(candidate,ensure_ascii=False)
    llm._stream_bounded=generate;e.llm=llm
    with pytest.raises(StoryContextError,match='story_context_budget'):
        await PersonalStoryService(e).render('лес','3-5',(),1,lambda:True)
    assert len(sent)==1 and not e.tts.calls


@pytest.mark.asyncio
@pytest.mark.parametrize('stop',['cancel','deadline'])
async def test_actual_application_cancel_or_original_deadline_revokes_repair(stop):
    from test_personal_story_app import start_app,enter,send
    app=await start_app();entered=asyncio.Event();pending=None;calls=[];deadline=[]
    clock=[app.monotonic_now()];app.monotonic_now=lambda:clock[0]
    async def generate(*args):
        calls.append(True)
        if len(calls)==1:yield wire(SHORT_PLOT,'3-5')
        else:
            owner=app._personal_stories['story-test'];deadline.append(owner.deadline)
            entered.set()
            if stop=='cancel':await asyncio.Future()
            else:
                clock[0]=owner.deadline
                async for value in LLM().stream_story(*args):yield value
    app.bounded_s2s.llm.stream_story=generate
    try:
        await enter(app)
        pending=asyncio.create_task(send(app,'ButtonPressed',{'button_id':1}))
        await asyncio.wait_for(entered.wait(),2)
        if stop=='cancel':assert (await send(app,'ButtonPressed',{'button_id':7}))['reason']=='activity_back'
        await asyncio.wait_for(pending,2)
        assert len(calls)==2 and deadline
        assert not app.bounded_s2s.tts.calls and not app._personal_story_jobs
        assert not any(item.get('response_id')=='PERSONAL_STORY' for item in app.audio.commands)
    finally:
        if pending:pending.cancel();await asyncio.gather(pending,return_exceptions=True)
        await app.shutdown_identity()
