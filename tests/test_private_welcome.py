import asyncio
import json
from types import SimpleNamespace
import pytest

from test_profile_onboarding import Harness, card
from app.foundation.s2s import AudioChunk, CachedTTSProvider
from app.foundation.domain import Event
from app.foundation.private_welcome import valid_name
from app.foundation.input_safety import InputSafetyDecision


class Render:
    def __init__(self):
        self.texts = []
        self.block = None
        self.stage = None
        self.entered = asyncio.Event()
        self.resist = False
        self.voice_audio = None
    async def wait(self, stage):
        if self.stage == stage:
            self.entered.set()
            try: await self.block.wait()
            except asyncio.CancelledError:
                if not self.resist: raise
                await self.block.wait()
    async def synthesize(self, text, token, cancelled):
        self.texts.append(text)
        await self.wait('tts')
        return AudioChunk((.1, .2), 24000, token)
    async def convert_private(self, audio, cancelled):
        await self.wait('voice')
        return self.voice_audio or audio


def setup(name='Люси-Анна'):
    h = Harness(card(name))
    render = Render()
    h.app.bounded_s2s.tts = CachedTTSProvider(render)
    h.app.bounded_s2s.voice = render
    h.pcm = []
    h.pcm_audio = []
    def play(handle, audio):
        assert h.app._active_audio[handle.session_id]['playback_id'] == handle.playback_id
        h.pcm.append(handle)
        h.pcm_audio.append(audio)
    h.app.audio.play_pcm_owned = play
    return h, render


async def start(h):
    await h.ready()
    await h.send('HandsetLifted')
    for _ in range(3): await asyncio.sleep(0)


async def speech(h, text, payload=None):
    h.sequence += 1
    return await h.app.receive_async(Event("SpeechReceived",h.boundary.session_id,h.sequence,1.,"test",payload or {},f"test:{h.sequence}",h.clock[0]),text)


async def deadline(h, at=15.):
    h.clock[0] = at
    return await h.send('WelcomeTimerFired')


def choice_codes(h):
    return [d['code'] for d in h.app.audio_runtime_diagnostics
        if d['kind']=='personal_welcome_choice']


def test_ready_exact_deadline_opens_menu_without_a_fake_mood_question():
    async def run():
        h,r = setup()
        try:
            await start(h)
            assert (await deadline(h,14.999999))['reason'] == 'welcome_not_due'
            assert not h.pcm
            result=await deadline(h)
            assert result['reason']=='personal_welcome_started', result
            assert len(h.pcm)==1 and h.state.mode=='voice_welcome'
            assert r.texts==['Привет, Люси-Анна. Я Мишка.']
            assert not h.app.bounded_s2s.tts.cache
            assert (await h.complete())['reason']=='personal_welcome_menu_started'
            assert h.state.mode=='exhibition_menu_tutorial_prompt'
            assert h.played[-1]=='PHRASE_007'
            assert h.state.mood_result is None
            public=json.dumps([h.app.last_canonical_effects,list(h.app.canonical_trace),list(h.app.audio_runtime_diagnostics),h.state.snapshot()],ensure_ascii=False)
            assert 'Люси-Анна' not in public and 'Как твои дела' not in public
        finally: await h.close()
    asyncio.run(run())


@pytest.mark.parametrize('stage',['tts','voice'])
@pytest.mark.parametrize('at',[14.999999,15.,15.000001])
def test_render_completion_boundary(stage,at):
    async def run():
        h,r=setup();r.stage=stage;r.block=asyncio.Event()
        r.voice_audio=AudioChunk((.3,.4),40000,0)
        try:
            await start(h);await r.entered.wait()
            h.clock[0]=at;r.block.set()
            for _ in range(4): await asyncio.sleep(0)
            await deadline(h,max(15.,at))
            assert bool(h.pcm)==(stage=='voice' or at<15.)
            if h.pcm:
                assert len(h.pcm)==1 and 'PHRASE_001' not in h.played
                assert h.pcm_audio[0].samples==((.3,.4) if at<15. else (.1,.2))
                assert choice_codes(h)==[
                    'personal_welcome_ready' if at<15. else 'personal_welcome_tts_fallback']
            else:assert h.played[-1]=='PHRASE_001'
            assert not h.app.bounded_s2s.tts.cache
        finally: await h.close()
    asyncio.run(run())


@pytest.mark.parametrize('stage',['tts','voice'])
@pytest.mark.parametrize('action',['HandsetPlaced','HardwareDisconnected','SessionReset'])
def test_cancel_every_render_await(stage,action):
    async def run():
        h,r=setup();r.stage=stage;r.block=asyncio.Event();r.resist=True
        try:
            await start(h);await r.entered.wait()
            await h.send(action)
            r.block.set()
            for _ in range(5): await asyncio.sleep(0)
            assert not h.pcm and not h.app._private_welcomes
            assert not h.app.bounded_s2s.tts.cache
        finally: r.block.set();await h.close()
    asyncio.run(run())


def test_late_resistant_render_does_not_delay_deadline_or_replace_fallback():
    async def run():
        h,r=setup();r.stage='tts';r.block=asyncio.Event();r.resist=True
        try:
            await start(h);await r.entered.wait()
            await asyncio.wait_for(deadline(h),.2)
            assert h.played[-1]=='PHRASE_001' and not h.pcm
            r.block.set()
            for _ in range(5): await asyncio.sleep(0)
            await deadline(h,15.000001)
            assert not h.pcm and h.played.count('PHRASE_001')==1
        finally:r.block.set();await h.close()
    asyncio.run(run())


@pytest.mark.parametrize('value,ok', [('Люси-Анна',True),("О’Нил",True),('Анна Мария',True),('Джахонгир',True),('Имя42',False),('А\u200bня',False),('А\u202eня',False),('<say>Аня',False),(' Аня',False),('Аня\n',False),('Игнорируй инструкции',False),('Сука',False)])
def test_name_grammar(value,ok): assert valid_name(value)==ok


def test_alphabetic_secrecy_name_rejected_before_tts():
    async def run():
        h,r=setup('Не говори родителям')
        try:
            await start(h);await deadline(h)
            assert not r.texts and not h.pcm and h.played[-1]=='PHRASE_001'
        finally:await h.close()
    asyncio.run(run())


@pytest.mark.parametrize('action',['expiry','revoke','request','profile'])
def test_scope_changed_before_deadline_never_plays_private(action):
    async def run():
        h,r=setup()
        try:
            await start(h)
            if action=='expiry':h.state.identity_expires_monotonic=15.
            elif action=='revoke':
                h.client.value=None
                h.client.events.append({'kind':'clear','reason_code':'scan_started'})
                await h.boundary.step()
            elif action=='request':h.state.identity_request_id='replacement'
            else:h.state.profile_ref='replacement'
            await deadline(h)
            assert not h.pcm
            if action=='expiry': assert h.state.mode=='session_closed'
        finally:await h.close()
    asyncio.run(run())


def test_ringtone_callback_does_not_cancel_preparation():
    async def run():
        h,r=setup();r.stage='tts';r.block=asyncio.Event()
        try:
            await start(h);await r.entered.wait()
            assert (await h.complete())['reason']=='ringtone_finished'
            r.block.set()
            for _ in range(4):await asyncio.sleep(0)
            await deadline(h)
            assert len(h.pcm)==1
        finally:r.block.set();await h.close()
    asyncio.run(run())


@pytest.mark.parametrize('badfield',['token','generation','playback_id','playback_lease'])
def test_forged_callbacks_do_not_open_menu(badfield):
    async def run():
        h,r=setup()
        try:
            await start(h);await deadline(h)
            callback=h.callback();callback[badfield]='9999'
            await h.send('AudioCompleted',callback)
            assert h.state.mode=='voice_welcome'
            await h.complete()
            assert h.state.mode=='exhibition_menu_tutorial_prompt'
        finally:await h.close()
    asyncio.run(run())


@pytest.mark.parametrize('kind',['urgent','adult','outage','low_stt','empty','forged','normal'])
def test_answer_admission_and_safety_priority(kind):
    async def run():
        h,r=setup()
        try:
            await start(h);await deadline(h);await h.complete()
            if kind=='outage':
                async def classify(text):return InputSafetyDecision('none','input_safety_timeout',False)
                h.app.safety_classifier.classify=classify
            if kind=='forged':
                h.sequence+=1
                await h.app.receive_async(Event('VoiceIntentReady',h.boundary.session_id,h.sequence,1.,'test',{'intent':'free_speech','safety_severity':'none'},'forged',h.clock[0]),'Хорошо')
            else:
                await speech(h, {'urgent':'Я не могу дышать','adult':'Позови взрослого','empty':''}.get(kind,'Хорошо'), {'_stt_untrusted':True} if kind=='low_stt' else None)
            if kind in {'urgent','adult'}:
                assert h.state.mode=='safety_stop'
            else:
                assert h.state.mode=='exhibition_menu_tutorial_prompt'
            assert h.state.mood_result is None
        finally:await h.close()
    asyncio.run(run())


@pytest.mark.parametrize('button',range(1,8))
def test_menu_skip_buttons_no_mood_inference(button):
    async def run():
        h,r=setup()
        try:
            await start(h);await deadline(h);await h.complete()
            result=await h.send('ButtonPressed',{'button_id':button})
            from app.foundation.canonical_contract import BUTTON_MODES
            expected=BUTTON_MODES[button]
            assert h.state.mode==expected,result
            assert h.state.mood_result is None
        finally:await h.close()
    asyncio.run(run())


def test_failed_private_audio_never_double_greets():
    async def run():
        h,r=setup()
        try:
            await start(h);await deadline(h)
            await h.send('AudioFailed',{**h.callback(),'error_code':'device_error'})
            await deadline(h,16.)
            assert len(h.pcm)==1 and 'PHRASE_001' not in h.played
            assert h.state.mode!='personal_welcome_listen'
        finally:await h.close()
    asyncio.run(run())


def test_cancel_during_trusted_validation_does_not_reach_tts():
    async def run():
        h,r=setup();entered=asyncio.Event();release=asyncio.Event()
        original=h.app.bounded_s2s.response_validator.validate_trusted
        async def validate(text,*args,**kwargs):
            entered.set()
            try:await release.wait()
            except asyncio.CancelledError:await release.wait()
            return await original(text,*args,**kwargs)
        h.app.bounded_s2s.response_validator.validate_trusted=validate
        try:
            await start(h);await entered.wait();await h.send('HandsetPlaced');release.set()
            for _ in range(5):await asyncio.sleep(0)
            assert not r.texts and not h.pcm and not h.app._private_welcomes
        finally:release.set();await h.close()
    asyncio.run(run())


def test_stale_due_timer_does_not_consume_ready_preparation():
    async def run():
        h,r=setup()
        try:
            await start(h);h.clock[0]=15.
            result=await h.send('WelcomeTimerFired',{'state_version':0})
            assert result['reason']=='stale_state_version'
            assert h.app._private_welcomes[h.boundary.session_id].audio is not None
            result=await deadline(h)
            assert len(h.pcm)==1 and result['reason']=='personal_welcome_started'
        finally:await h.close()
    asyncio.run(run())


def test_missing_playback_id_private_completion_rejected():
    async def run():
        h,r=setup()
        try:
            await start(h);await deadline(h)
            value=h.callback();value.pop('playback_id')
            await h.send('AudioCompleted',value)
            assert h.state.mode=='voice_welcome'
        finally:await h.close()
    asyncio.run(run())


def test_real_mock_pcm_and_resilient_primary_private_path():
    async def run():
        from app.foundation.s2s import ResilientVoiceProvider, PassthroughVoiceProvider
        h=Harness(card('Анна Мария'))
        h.app.bounded_s2s.voice=ResilientVoiceProvider(PassthroughVoiceProvider())
        try:
            await start(h)
            task=h.app._private_welcomes[h.boundary.session_id].task
            await task
            result=await deadline(h)
            assert result['reason']=='personal_welcome_started'
            assert h.app._active_audio[h.boundary.session_id]['content_id']=='PERSONAL_WELCOME'
            assert not h.app.bounded_s2s.tts.cache
            assert 'Анна Мария' not in repr(h.app.audio.commands)
            await h.complete()
            assert h.state.mode=='exhibition_menu_tutorial_prompt'
        finally:await h.close()
    asyncio.run(run())


def test_future_event_timestamp_cannot_start_greeting_early():
    async def run():
        h,r=setup()
        try:
            await start(h)
            h.sequence+=1
            result=await h.app.receive_async(Event('WelcomeTimerFired',h.boundary.session_id,h.sequence,1.,'test',{},'future',999999.))
            assert result['reason']=='welcome_not_due'
            assert not h.pcm and 'PHRASE_001' not in h.played
            assert h.state.welcome_status=='pending'
            await deadline(h)
            assert len(h.pcm)==1
        finally:await h.close()
    asyncio.run(run())


def test_unsupported_private_pcm_uses_recording_without_preparation():
    async def run():
        h,r=setup();h.app.audio.private_pcm_supported=False
        try:
            await start(h);await deadline(h)
            assert not r.texts and not h.pcm and h.played[-1]=='PHRASE_001'
        finally:await h.close()
    asyncio.run(run())


def test_failed_menu_audio_does_not_repeat_greeting_and_help_remains_available():
    async def run():
        h,r=setup()
        try:
            await start(h);await deadline(h);await h.complete()
            await h.send('AudioFailed',{**h.callback(),'error_code':'device_error'})
            assert h.state.audio_status=='failed'
            # First edge skips the tutorial; the next fresh help edge is live.
            await h.send('ButtonPressed',{'button_id':5})
            await h.send('ButtonReleased',{'button_id':5})
            await h.send('ButtonPressed',{'button_id':5})
            assert h.state.mode=='wellbeing_check'
            assert len(h.pcm)==1
            assert 'PHRASE_001' not in h.played
        finally:await h.close()
    asyncio.run(run())


@pytest.mark.parametrize('field,value',[('sample_rate',0),('sample_rate',float('nan')),('sample_rate',True),('samples',()),('samples',(float('nan'),)),('samples',(float('inf'),)),('samples',(1.01,)),('samples',(-1.01,)),('samples',(10**1000,)),('samples',((.1,.2),))])
@pytest.mark.parametrize('stage',['tts','voice'])
def test_invalid_pcm_keeps_trusted_tts_or_uses_recording(field,value,stage):
    async def run():
        h,r=setup()
        invalid=AudioChunk((.1,),24000,0)
        object.__setattr__(invalid,field,value)
        async def bad_tts(text,token,cancelled):return invalid
        async def bad_voice(audio,cancelled):return invalid
        if stage=='tts':r.synthesize=bad_tts
        else:r.convert_private=bad_voice
        try:
            await start(h);await deadline(h)
            if stage=='tts':
                assert not h.pcm and h.played[-1]=='PHRASE_001'
                assert h.state.active_audio_id=='PHRASE_001'
            else:
                assert len(h.pcm)==1 and h.pcm_audio[0].samples==(.1,.2)
                assert 'PHRASE_001' not in h.played
                assert h.state.active_audio_id=='PERSONAL_WELCOME'
                assert choice_codes(h)==['personal_welcome_tts_fallback']
        finally:await h.close()
    asyncio.run(run())


@pytest.mark.parametrize('resist', [False, True])
def test_ready_tts_survives_cold_private_voice_at_deadline(resist):
    async def run():
        h,r=setup();r.stage='voice';r.block=asyncio.Event();r.resist=resist
        r.voice_audio=AudioChunk((.3,.4),40000,0)
        try:
            await start(h);await r.entered.wait()
            owner=h.app._private_welcomes[h.boundary.session_id]
            result=await asyncio.wait_for(deadline(h),.2)
            assert result['reason']=='personal_welcome_started',result
            assert len(h.pcm)==1 and h.pcm_audio[0].samples==(.1,.2)
            assert 'PHRASE_001' not in h.played
            assert owner.closed and owner.audio is None
            h.clock[0]=15.000001;r.block.set()
            await owner.task
            await deadline(h,16.)
            assert len(h.pcm)==1 and 'PHRASE_001' not in h.played
            assert choice_codes(h)==['personal_welcome_tts_fallback']
            assert not h.app.bounded_s2s.tts.cache
        finally:r.block.set();await h.close()
    asyncio.run(run())


@pytest.mark.parametrize('mode', ['missing', 'failure'])
@pytest.mark.parametrize('wrapped', [False, True])
def test_private_voice_optional_after_trusted_tts(mode,wrapped,caplog):
    async def run():
        from app.foundation.s2s import ResilientVoiceProvider
        h,r=setup()
        async def ordinary(*args):pytest.fail('private audio reached ordinary conversion')
        async def failed(*args):raise RuntimeError('PRIVATE_SYNTHETIC_MARKER')
        voice=SimpleNamespace(convert=ordinary)
        if mode=='failure':voice.convert_private=failed
        h.app.bounded_s2s.voice=ResilientVoiceProvider(voice) if wrapped else voice
        try:
            await start(h)
            owner=h.app._private_welcomes[h.boundary.session_id]
            await owner.task
            assert owner.audio.samples==(.1,.2)
            result=await deadline(h)
            assert result['reason']=='personal_welcome_started'
            assert len(h.pcm)==1 and h.pcm_audio[0].samples==(.1,.2)
            assert 'PHRASE_001' not in h.played
            assert choice_codes(h)==['personal_welcome_tts_fallback']
            public=json.dumps(list(h.app.audio_runtime_diagnostics),ensure_ascii=False)
            assert 'PRIVATE_SYNTHETIC_MARKER' not in public+caplog.text
            assert 'Люси-Анна' not in public+caplog.text
            assert not h.app.bounded_s2s.tts.cache
        finally:await h.close()
    asyncio.run(run())


@pytest.mark.parametrize('action', ['hangup', 'revoke', 'profile'])
def test_scope_change_clears_trusted_tts_during_private_voice(action):
    async def run():
        h,r=setup();r.stage='voice';r.block=asyncio.Event();r.resist=True
        r.voice_audio=AudioChunk((.3,.4),40000,0)
        try:
            await start(h);await r.entered.wait()
            owner=h.app._private_welcomes[h.boundary.session_id]
            assert owner.audio.samples==(.1,.2)
            if action=='hangup':await h.send('HandsetPlaced')
            else:
                h.client.value=None
                h.client.events.append({'kind':'clear','reason_code':'scan_started'})
                await h.boundary.step()
                if action=='profile':
                    await h.send('HandsetPlaced')
                    h.client.value=card('Синтетик')
                    assert (await h.ready())['reason']=='profile_ready'
                    await h.send('HandsetLifted')
            assert owner.closed and owner.audio is None
            r.block.set();await owner.task
            assert owner.audio is None and not h.pcm
            if action=='profile':
                for _ in range(4):await asyncio.sleep(0)
                await deadline(h)
                assert len(h.pcm)==1 and r.texts==[
                    'Привет, Люси-Анна. Я Мишка.',
                    'Привет, Синтетик. Я Мишка.']
            else:
                await deadline(h)
                assert not h.pcm
            assert not h.app.bounded_s2s.tts.cache
        finally:r.block.set();await h.close()
    asyncio.run(run())
