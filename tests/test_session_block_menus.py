"""Temporary honest story-menu fallback, not completed narrated-menu content."""
from pathlib import Path
import pytest
from app.foundation.canonical_contract import CanonicalEvent, CanonicalRuntime, CanonicalState


def setup(mode='menu_ready'):
    r=CanonicalRuntime(exhibition_mode=True)
    r.sessions['s']=CanonicalState('s',handset='up',mode=mode)
    return r


def send(r,kind,payload=None,text=None):
    s=r.sessions['s'];seq=s.last_sequence_by_source.get('test',0)+1
    return r.handle(CanonicalEvent(kind,'s',seq,s.state_version,'test',payload or {},f'e{seq}'),transient_text=text)


def complete(r):
    s=r.sessions['s']
    return send(r,'AudioCompleted',{'response_id':s.active_audio_id,'playback_lease':s.playback_lease})


@pytest.mark.parametrize('entry',['menu_ready','root_menu'])
def test_no_five_story_promise_and_no_fake_repeat(entry):
    r=setup(entry);result=send(r,'ButtonPressed',{'button_id':2})
    assert [x['phrase_id'] for x in result['effects'] if x['type']=='speak']==['PHRASE_075']
    visual=next(x for x in result['effects'] if x['type']=='visual')
    assert visual['buttons']=={'1':'fixed_aibolit_v1','2':'fixed_squirrel_v1','3':'fixed_lukomorye_v1','4':'fixed_soft_paws_star_v1','5':'help','7':'back'}
    assert any(x.get('error_code')=='story_menu_audio_mapping_conflict' for x in result['effects'])
    assert complete(r)['reason']=='story_menu_hint_finished'
    assert not r.sessions['s'].active_audio_id
    send(r,'ButtonPressed',{'button_id':7})
    result=send(r,'ButtonPressed',{'button_id':2})
    assert not any(x['type']=='speak' for x in result['effects'])
    assert send(r,'VoiceIntentReady',{'intent':'yes','safety_severity':'none'},'Да')['reason']!='block_intro_replay'


@pytest.mark.parametrize('button',[1,2,3,4,6,7])
def test_hint_any_button_interrupt_and_stale_audio_inert(button):
    r=setup();send(r,'ButtonPressed',{'button_id':2})
    s=r.sessions['s'];old={'response_id':s.active_audio_id,'playback_lease':s.playback_lease}
    result=send(r,'ButtonPressed',{'button_id':button})
    assert result['reason']=='story_menu_hint_skipped'
    assert result['state']['mode']=='fixed_story_menu'
    assert send(r,'ButtonPressed',{'button_id':button})['reason']=='story_menu_hint_skip_held'
    assert not any(x['type']=='speak' for x in send(r,'AudioCompleted',old)['effects'])
    send(r,'ButtonReleased',{'button_id':button})
    if button==5:
        result=send(r,'ButtonPressed',{'button_id':5})
        assert result['reason']=='help_priority' and result['state']['mode']=='wellbeing_check'


def test_help_and_back_mapping_after_hint():
    for button,reason,mode in [(5,'help_priority','wellbeing_check'),(7,'story_menu_back','root_menu')]:
        r=setup();send(r,'ButtonPressed',{'button_id':2});complete(r)
        result=send(r,'ButtonPressed',{'button_id':button})
        assert result['reason']==reason and result['state']['mode']==mode


@pytest.mark.parametrize('button',range(1,5))
def test_visual_story_mapping_matches_actual_selection(button):
    r=setup();first=send(r,'ButtonPressed',{'button_id':2});complete(r)
    visual=next(x for x in first['effects'] if x['type']=='visual')
    result=send(r,'ButtonPressed',{'button_id':button})
    assert any(x.get('story_id')==visual['buttons'][str(button)] for x in result['effects'])


def test_session_and_identity_reset_restore_one_time_hint():
    r=setup();send(r,'ButtonPressed',{'button_id':2});send(r,'HandsetPlaced')
    assert not r.sessions['s'].story_menu_hint_seen
    s=r.sessions['s'];s.mode='menu_ready';s.handset='up'
    send(r,'ButtonPressed',{'button_id':2});r.clear_identity(s)
    assert not s.story_menu_hint_seen and not s.story_menu_hint_active


@pytest.mark.parametrize('button',range(1,8))
@pytest.mark.parametrize('phase',['exhibition_menu_tutorial_buttons','exhibition_menu_repeat_prompt','exhibition_menu_repeat_listen'])
def test_existing_root_menu_any_button_still_skips(button,phase):
    r=setup(phase);result=send(r,'ButtonPressed',{'button_id':button})
    assert result['reason']=='exhibition_menu_tutorial_skipped'
    assert result['state']['mode']=='menu_ready'
    assert any(x['type']=='audio_stop' for x in result['effects'])


@pytest.mark.parametrize('answer',['yes','no'])
def test_existing_root_repeat_choice_and_reentry(answer):
    r=setup('exhibition_menu_repeat_listen')
    result=send(r,'VoiceIntentReady',{'intent':answer,'safety_severity':'none'})
    if answer=='yes':
        assert result['reason']=='exhibition_menu_replay_started'
        complete(r)
        result=send(r,'ExhibitionMenuWaveComplete')
        assert result['reason']=='exhibition_menu_replay_complete'
    else: assert result['reason']=='exhibition_menu_repeat_declined'
    assert result['state']['mode']=='menu_ready'
    assert not any(x.get('phrase_id')=='PHRASE_009' for x in result['effects'])


def test_console_mapping_is_honest_without_unapproved_audio():
    text=Path('app/foundation/console/console.js').read_text()
    assert 'Выберите сказку кнопками 1–5' not in text
    assert 'Выберите одну из пяти' not in text
    assert '5 — помощь; 7 — назад. Озвучка меню временно недоступна.' in text


@pytest.mark.asyncio
async def test_actual_application_hint_completion_and_help():
    from app.foundation.api import build_application
    from app.foundation.config import Settings
    from app.foundation.domain import Event
    app,*_=build_application(settings=Settings(profile='mock'),background_scheduler=False)
    seq=0
    async def dispatch(kind,payload=None):
        nonlocal seq
        seq+=1
        return await app.receive_async(Event(kind,'menus',seq,1.,'menus',payload or {},f'menus:{seq}',1.))
    try:
        await dispatch('HandsetLifted');app.canonical_runtime.sessions['menus'].mode='menu_ready'
        result=await dispatch('ButtonPressed',{'button_id':2})
        lease=dict(app._active_audio['menus'])
        assert lease['content_id']=='PHRASE_075'
        callback={key:lease[key] for key in ('token','generation','playback_lease','playback_id')};callback['response_id']='PHRASE_075'
        assert (await dispatch('AudioCompleted',callback))['reason']=='story_menu_hint_finished'
        assert 'menus' not in app._active_audio
        result=await dispatch('ButtonPressed',{'button_id':5})
        assert result['reason']=='help_priority'
        assert app._active_audio['menus']['content_id']=='PHRASE_084'
        await dispatch('HandsetPlaced')
        assert 'menus' not in app._active_audio
    finally: await app._cleanup_session_resources('menus')


@pytest.mark.asyncio
async def test_help_button_immediately_interrupts_hint_and_old_callback_cannot_stop_help():
    from app.foundation.api import build_application
    from app.foundation.config import Settings
    from app.foundation.domain import Event
    app,*_=build_application(settings=Settings(profile="mock"),background_scheduler=False)
    def event(kind,seq,payload=None):
        return Event(kind,"hint-help",seq,1.,"test",payload or {},f"hint-help:{seq}",1.)
    try:
        await app.receive_async(event("HandsetLifted",1))
        app.canonical_runtime.sessions["hint-help"].mode="menu_ready"
        await app.receive_async(event("ButtonPressed",2,{"button_id":2}))
        lease=dict(app._active_audio["hint-help"])
        old={key:lease[key] for key in ("token","generation","playback_lease","playback_id")}
        old["response_id"]="PHRASE_075"
        result=await app.receive_async(event("ButtonPressed",3,{"button_id":5}))
        assert result["reason"]=="help_priority"
        assert app.canonical_runtime.sessions["hint-help"].mode=="wellbeing_check"
        assert app._active_audio["hint-help"]["content_id"]=="PHRASE_084"
        await app.receive_async(event("AudioCompleted",4,old))
        assert app._active_audio["hint-help"]["content_id"]=="PHRASE_084"
    finally:
        await app._cleanup_session_resources("hint-help")
