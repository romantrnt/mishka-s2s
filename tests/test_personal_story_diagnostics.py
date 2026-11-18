"""Local stage logs expose bounded enums/counts, never story/identity payloads."""
import asyncio
import json
import logging

import pytest

from app.foundation.personal_story import PersonalStoryService
from app.foundation.story_context import StoryContextError
from test_personal_story_app import start_app, enter, send
from test_personal_story_service import engine, TEXT

PRIVATE='PRIVATE_FIXTURE_NEVER_LOG'


def diagnostics(caplog):
    records=[record for record in caplog.records if record.name=='mishka.personal_story']
    assert records and all(record.levelno==logging.WARNING and not record.exc_info for record in records)
    output='\n'.join(record.getMessage() for record in records)
    assert PRIVATE not in output and TEXT not in output and 'story-test' not in output
    values=[json.loads(record.getMessage().removeprefix('personal_story ')) for record in records]
    assert all(type(value['attempt']) is int and value['attempt']>0 for value in values)
    assert not any(set(value)&{'token','session_id','identity','profile','prompt','text','samples_data','request_id'} for value in values)
    return values


def test_logger_whitelist_drops_unknown_keys_and_arbitrary_strings(caplog):
    service=PersonalStoryService(engine())
    service._diagnostic(PRIVATE,PRIVATE,band=PRIVATE,error=PRIVATE,code=PRIVATE,
                        session_id=PRIVATE,token=999,private_number=999,characters=12)
    value=diagnostics(caplog)[0]
    assert value=={'attempt':value['attempt'],'stage':'unknown','reason':'unknown','characters':12}


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['private_pcm','generator','validator','jobs','band','exception','backend'])
async def test_prepare_rejection_is_specific_and_cannot_log_private_fixture(caplog,failure):
    app=await start_app();job=None
    try:
        await enter(app)
        state=app.canonical_runtime.sessions['story-test']
        app._canonical_profiles['story-test']={'display_name':PRIVATE}
        if failure=='private_pcm':app.audio.private_pcm_supported=False
        if failure=='generator':app.bounded_s2s.llm.supports_full_story=False
        if failure=='validator':app.bounded_s2s.response_validator.mini.supports_full_story=False
        if failure=='jobs':
            job=asyncio.create_task(asyncio.sleep(60),name=PRIVATE)
            app._personal_story_jobs.add(job)
        if failure=='band':state.age_band=PRIVATE
        if failure in {'exception','backend'}:
            async def failed(*args):
                if failure=='backend':raise StoryContextError('story_backend_changed',PRIVATE)
                raise type(PRIVATE,(RuntimeError,),{})(PRIVATE)
                yield ''
            app.bounded_s2s.llm.stream_story=failed
            await send(app,'ButtonPressed',{'button_id':1})
        else:
            assert await app._prepare_personal_story('story-test',{'theme':'лес','private':PRIVATE},999) is None
        values=diagnostics(caplog)
        last=values[-1]
        assert last['reason']==('jobs_busy' if failure=='jobs' else 'exception' if failure in {'exception','backend'} else 'admission_rejected')
        if failure=='private_pcm':assert last['private_pcm_available'] is False
        if failure=='generator':assert last['generator_capable'] is False
        if failure=='validator':assert last['validator_capable'] is False
        if failure=='band':assert last['band_valid'] is False and 'band' not in values[0]
        if failure=='jobs':assert last['jobs']==1 and last['jobs_done']==0
        if failure=='exception':assert last['error']=='other' and 'code' not in last
        if failure=='backend':assert last['error']=='StoryContextError' and last['code']=='story_backend_changed'
    finally:
        if job:
            job.cancel();await asyncio.gather(job,return_exceptions=True)
        await app.shutdown_identity()


@pytest.mark.asyncio
@pytest.mark.parametrize('duration,expected',[(57.45,'duration_rejected'),(80,'ready')])
async def test_render_reports_last_gate_and_numeric_pcm_only(caplog,duration,expected):
    e=engine(duration=duration)
    await PersonalStoryService(e).render('лес','3-5',(),999,lambda:True)
    values=diagnostics(caplog)
    assert len({value['attempt'] for value in values})==1
    assert values[-1]['stage']=='pcm' and values[-1]['reason']==expected
    assert values[-1]['samples']>0 and values[-1]['rate']==1000
    assert [value['stage'] for value in values]==['generation','structure','semantic','tts','pcm']
