import pytest
from app.foundation.canonical_contract import CanonicalRuntime,CanonicalState,CanonicalEvent

@pytest.mark.parametrize('phase',['narrating','repeat_prompt','repeat_listen'])
@pytest.mark.parametrize('severity',['concern','urgent'])
def test_menu_phase_never_swallows_existing_safety_path(phase,severity):
    runtime=CanonicalRuntime()
    state=CanonicalState('s',mode='fixed_story_menu',handset='up')
    state.scene_menu_active=state.mode;state.scene_menu_phase=phase
    runtime.sessions['s']=state
    result=runtime.handle(CanonicalEvent('VoiceIntentReady','s',1,0,'test',{'intent':'free_speech','safety_severity':severity},'safety'))
    assert result['reason']==('concern_support' if severity=='concern' else 'urgent_safety')
    assert state.mode==('support_intake' if severity=='concern' else 'safety_stop')

@pytest.mark.parametrize('phase',['narrating','repeat_prompt'])
def test_ordinary_speech_during_menu_audio_cannot_route(phase):
    runtime=CanonicalRuntime();state=CanonicalState('s',mode='fixed_story_menu',handset='up')
    state.scene_menu_active=state.mode;state.scene_menu_phase=phase;runtime.sessions['s']=state
    result=runtime.handle(CanonicalEvent('VoiceIntentReady','s',1,0,'test',{'intent':'yes','safety_severity':'none'},'ordinary'))
    assert result['reason']=='scene_menu_not_listening'
    assert state.scene_menu_phase==phase
