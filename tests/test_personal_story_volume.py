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


def long_sentence_parts(*,short=False,words=12):
    parts=[' '.join(part[:2 if short else 5]) for part in PLOT_SENTENCES]
    sentence='Зайчик нашёл под большим зелёным листом маленькое семечко и позвал соседку белочку.'
    if words==9:sentence='Зайчик нашёл под зелёным листом семечко и позвал белочку.'
    if words==16:sentence=sentence[:-1]+' возле знакомой лесной тропинки.'
    if words==19:sentence=sentence[:-1]+' чтобы вместе посадить его на солнечной поляне.'
    assert len(sentence.split())==words
    parts[0]=parts[0].replace(PLOT_SENTENCES[0][0],sentence)
    return parts


def old_volume_parts(expanded=False):
    counts=(7,5,4,7) if expanded else (7,4,4,5)
    parts=[' '.join(part[:count]) for part,count in zip(PLOT_SENTENCES,counts)]
    if expanded:parts[3]=parts[3].replace('его нежными ','')
    else:parts[0]=parts[0].replace('старого ','')
    assert len(' '.join(re.findall(r'[а-яё]+',' '.join(parts),re.I)))==(1051 if expanded else 909)
    return parts


@pytest.mark.asyncio
@pytest.mark.parametrize('also_short',[False,True])
async def test_young_twelve_word_sentence_and_volume_share_one_complete_repair(caplog,also_short):
    e=engine('1-3');calls=[];original=e.llm.stream_story
    async def generate(messages,request_id,cancelled):
        calls.append(messages)
        if len(calls)==1:yield wire(long_sentence_parts(short=also_short),'1-3')
        else:
            feedback=json.loads(messages[-1]['content'])
            assert feedback['max_sentence_words']==12 and feedback['allowed_sentence_words']==8
            assert feedback['desired_sentence_words']==4
            assert (feedback['measured_russian_characters']<feedback['required_minimum']) is also_short
            assert bool(sum(feedback['additional_sentences_per_part'].values())) is also_short
            async for value in original(messages,request_id,cancelled):yield value
    e.llm.stream_story=generate
    audio=await PersonalStoryService(e).render('лес','1-3',(),1,lambda:True)
    assert audio and audio.duration_ms==45000 and len(calls)==2
    assert ' '.join(e.tts.calls)==story_text('1-3')
    logs=[json.loads(r.getMessage().removeprefix('personal_story ')) for r in caplog.records
          if r.name=='mishka.personal_story']
    repair=next(v for v in logs if v['reason']=='sentence_long')
    assert repair['max_sentence_words']==12
    assert long_sentence_parts(short=also_short)[0] not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize('remaining',['sentence','volume'])
async def test_second_sentence_or_volume_violation_cannot_start_another_repair(remaining):
    e=engine('1-3');calls=[]
    async def generate(*args):
        calls.append(True)
        parts=(long_sentence_parts(short=len(calls)==1) if len(calls)==1 or remaining=='sentence'
               else [' '.join(part[:2]) for part in PLOT_SENTENCES])
        yield wire(parts,'1-3')
    e.llm.stream_story=generate
    assert await PersonalStoryService(e).render('лес','1-3',(),1,lambda:True) is None
    assert len(calls)==2 and not e.tts.calls


@pytest.mark.asyncio
async def test_balanced_1045_character_draft_reaches_measured_pcm_without_repair():
    parts=[' '.join(part[:count]) for part,count in zip(PLOT_SENTENCES,(6,6,6,4))]
    parts[0]=parts[0].replace('нашёл семечко','нашёл жёлтое семечко')
    text=' '.join(parts)
    assert len(' '.join(re.findall(r'[а-яё]+',text,re.I)))==1045
    e=engine('4-5',duration=75,text=text);calls=[]
    async def generate(*args):calls.append(True);yield wire(parts)
    e.llm.stream_story=generate
    audio=await PersonalStoryService(e).render('лес','4-5',(),1,lambda:True)
    assert audio and audio.duration_ms==75000 and len(calls)==1
    assert ' '.join(e.tts.calls)==text


@pytest.mark.asyncio
@pytest.mark.parametrize('expanded',[False,True])
@pytest.mark.parametrize('issue',['volume','sentence9','sentence16','old_volume'])
async def test_actual_ollama_http_transmits_measured_repair_feedback_without_replacing_messages(monkeypatch,expanded,issue):
    import httpx
    from app.foundation.s2s import OllamaLLMProvider
    from test_story_backend_context import URL,MODEL,contract
    sent=[];client=httpx.AsyncClient
    style=issue.startswith('sentence')
    words=int(issue.removeprefix('sentence')) if style else None
    band='1-3' if style else '4-5' if issue=='volume' else '6-7'
    first=long_sentence_parts(words=words) if style else SHORT_PLOT if issue=='volume' else old_volume_parts()
    class Body(httpx.AsyncByteStream):
        def __init__(self,raw):self.raw=raw
        async def __aiter__(self):yield self.raw
    def reply(request):
        sent.append(json.loads(request.content))
        count=5 if style else 7 if issue=='volume' else 10
        parts=[' '.join(part[:count]) for part in PLOT_SENTENCES] if expanded and len(sent)==2 else first
        if issue=='old_volume' and not expanded and len(sent)==2:parts=old_volume_parts(expanded=True)
        raw='\n'.join(map(json.dumps,[{'message':{'content':wire(parts,band)}},
                                    {'done':True,'done_reason':'stop'}])).encode()
        return httpx.Response(200,stream=Body(raw))
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:client(transport=httpx.MockTransport(reply),**kw))
    c=await contract();e=engine(band)
    e.llm=OllamaLLMProvider(URL,MODEL,1,num_ctx=16384,story_backend_contract=c)
    audio=await PersonalStoryService(e).render('лес',band,(),1,lambda:True)
    assert (audio is not None) is expanded
    assert len(sent)==2 and len(sent[0]['messages'])==1 and len(sent[1]['messages'])==2
    assert sent[0]['messages'][0]==sent[1]['messages'][0]
    message=sent[1]['messages'][-1]
    assert message['role']=='user'
    feedback=json.loads(message['content'])
    assert feedback['draft']==json.loads(wire(first,band)) and 'text' not in feedback['draft']
    minimum=14*DURATION_BOUNDS[band][0];target=7*sum(DURATION_BOUNDS[band])
    assert feedback['required_minimum']==minimum and feedback['desired_volume']==[target,target+target//8]
    assert (feedback['measured_russian_characters']<minimum) is (not style)
    assert feedback['max_sentence_words']==(words if style else 16 if issue=='volume' else 7)
    assert feedback['allowed_sentence_words']==(8 if style else 18 if issue=='volume' else 30)
    assert feedback['desired_sentence_words']==(4 if style else 9 if issue=='volume' else 15)
    additional=feedback['additional_sentences_per_part']
    assert set(additional)=={'setting','problem','attempt','resolution'}
    if issue=='old_volume':
        assert additional=={'setting':5,'problem':4,'attempt':4,'resolution':4}
    elif style:assert not any(additional.values())
    else:assert 4<=sum(additional.values())<=32 and all(0<=v<=8 for v in additional.values())
    if style:
        assert feedback['sentence_splits']=={'setting':[{'sentence_index':1,'observed_words':words,
                                                       'required_sentences':3 if words==9 else 4}]}
        assert isinstance(feedback['sentence_split_instruction'],str)
    else:
        assert 'sentence_splits' not in feedback and 'sentence_split_instruction' not in feedback
        assert feedback['revision_request']=='Revise the full draft within allowed_sentence_words and required_minimum, aiming for desired_sentence_words and desired_volume. Split long sentences into complete simple short sentences. Preserve the full plot and add additional_sentences_per_part distinct meaningful sentences/events in the named parts; zero means only split or rebalance existing text. Return revised full JSON, never an unchanged draft; keep age/theme/schema, no repetition, padding or fragments.'
    assert isinstance(feedback['revision_request'],str) and 0<len(feedback['revision_request'])<500
    assert all(item['shift'] is False and item['truncate'] is False
               and item['options']['num_ctx']==16384
               and item['format']==e.llm.story_response_schema_for_age(band) for item in sent)
    assert c.ready(URL,MODEL)
    assert bool(e.tts.calls) is expanded


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
            minimum=14*DURATION_BOUNDS[band][0];target=7*sum(DURATION_BOUNDS[band])
            assert feedback['measured_russian_characters']<feedback['required_minimum']==minimum
            assert feedback['desired_volume']==[target,target+target//8]
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
@pytest.mark.parametrize('invalid',['schema','unsafe'])
async def test_invalid_first_draft_is_not_repaired(invalid):
    e=engine();calls=[];candidate=json.loads(wire(SHORT_PLOT,'3-5'))
    if invalid=='schema':del candidate['title']
    if invalid=='unsafe':candidate['plot']['attempt']='Принимай таблетки.'
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
@pytest.mark.parametrize('stage',['trusted_validation','tts'])
async def test_actual_authored_application_cancel_or_original_deadline_revokes_render(stop,stage):
    from test_personal_story_app import start_app,enter,send
    app=await start_app();entered=asyncio.Event();pending=None;deadlines=[]
    clock=[app.monotonic_now()];app.monotonic_now=lambda:clock[0]
    provider=(app.bounded_s2s.response_validator if stage=='trusted_validation' else app.bounded_s2s.tts)
    method='validate_trusted' if stage=='trusted_validation' else 'synthesize'
    original=getattr(provider,method)
    async def blocked(*args,**kwargs):
        owner=app._personal_stories['story-test'];deadlines.append(owner.deadline)
        entered.set()
        if stop=='cancel':await asyncio.Future()
        else:clock[0]=owner.deadline
        return await original(*args,**kwargs)
    setattr(provider,method,blocked)
    try:
        await enter(app)
        pending=asyncio.create_task(send(app,'ButtonPressed',{'button_id':1}))
        await asyncio.wait_for(entered.wait(),2)
        if stop=='cancel':assert (await send(app,'ButtonPressed',{'button_id':7}))['reason']=='activity_back'
        await asyncio.wait_for(pending,2)
        assert len(deadlines)==1
        assert not app.bounded_s2s.llm.calls and not app._personal_story_jobs
        if stage=='trusted_validation':assert not app.bounded_s2s.tts.calls
        assert not any(item.get('response_id')=='PERSONAL_STORY' for item in app.audio.commands)
    finally:
        if pending:pending.cancel();await asyncio.gather(pending,return_exceptions=True)
        await app.shutdown_identity()
