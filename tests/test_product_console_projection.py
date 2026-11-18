import pytest
from app.foundation.config import Settings
from app.foundation.stand import HardwareIntegrationStand

@pytest.mark.asyncio
@pytest.mark.parametrize('substate,visible',[('game_exit_confirmation',True),('PRIVATE_INTERNAL_CONTEXT',False)])
async def test_operator_exit_guide_receives_only_allowlisted_public_substate(substate,visible):
    stand=HardwareIntegrationStand(settings=Settings(profile='mock'),background_scheduler=False)
    try:
        stand.application.last_canonical_state={'mode':'game_active','handset':'down','substate':substate}
        panel=stand.panel_snapshot()
        assert ('substate' in panel['canonical_state']) is visible
        if visible: assert panel['canonical_state']['substate']=='game_exit_confirmation'
        else: assert substate not in repr(panel)
    finally:await stand.aclose()

@pytest.mark.asyncio
async def test_operator_menu_exposes_only_catalogue_choices_and_context_controls():
    from app.foundation.canonical_contract import CanonicalState
    stand = HardwareIntegrationStand(settings=Settings(profile='mock'), background_scheduler=False)
    try:
        state = CanonicalState('stand', mode='game_menu', handset='up', age_band='5-7')
        page = stand.application.canonical_runtime._menu_page(state).as_dict()
        page['private_context'] = 'PRIVATE_SENTINEL'
        page['spoken_text'] = 'PRIVATE_SENTINEL'
        page['items'][0]['private_context'] = 'PRIVATE_SENTINEL'
        stand.application.last_canonical_state = {'mode': 'game_menu', 'scene_menu_descriptor': page}
        public = stand.panel_snapshot()['canonical_state']['scene_menu_descriptor']
        assert len(public['items']) == 5
        assert public['items'][4]['slot'] == 5
        assert set(public) == {'scene', 'items', 'buttons'}
        assert all(set(item) == {'slot', 'title'} for item in public['items'])
        assert 'PRIVATE_SENTINEL' not in repr(public)
        page['items'][0]['title'] = 'PRIVATE_SENTINEL'
        assert 'scene_menu_descriptor' not in stand.panel_snapshot()['canonical_state']
        page['scene'] = 'bear_dialogue'
        assert 'scene_menu_descriptor' not in stand.panel_snapshot()['canonical_state']
    finally:
        await stand.aclose()

@pytest.mark.asyncio
async def test_game_guide_receives_only_public_game_and_audio_enums():
    stand = HardwareIntegrationStand(settings=Settings(profile='mock'), background_scheduler=False)
    try:
        canonical = {
            'mode': 'game_active', 'active_game_id': 'word_game',
            'game_audio_stage': 'playing',
            'game_engine_state': {'phase': 'playing', 'private_context': 'PRIVATE_SENTINEL'},
            'game_parameters': {'private_context': 'PRIVATE_SENTINEL'},
        }
        stand.application.last_canonical_state = canonical
        public = stand.panel_snapshot()['canonical_state']
        assert public['active_game_id'] == 'word_game'
        assert public['game_audio_stage'] == public['game_phase'] == 'playing'
        assert 'PRIVATE_SENTINEL' not in repr(public)
        for key in ('active_game_id', 'game_audio_stage'):
            canonical[key] = 'PRIVATE_SENTINEL'
        canonical['game_engine_state']['phase'] = 'PRIVATE_SENTINEL'
        public = stand.panel_snapshot()['canonical_state']
        assert not {'active_game_id', 'game_audio_stage', 'game_phase'} & public.keys()
        canonical.update(mode='menu_ready', active_game_id='word_game', game_audio_stage='playing')
        canonical['game_engine_state']['phase'] = 'playing'
        assert not {'active_game_id', 'game_audio_stage', 'game_phase'} & stand.panel_snapshot()['canonical_state'].keys()
    finally:
        await stand.aclose()


def test_audio_failure_projection_keeps_only_fixed_diagnostic_values():
    public = HardwareIntegrationStand._panel_metrics({'events': [{
        'event': 'audio_generation_failed', 'at': 1., 'chunk_index': 1,
        'stage': 'silero_tts', 'exception_class': 'RuntimeError', 'cause_class': 'ValueError',
        'code': 'silero_empty_text', 'text_chars': 2, 'text': 'PRIVATE_SENTINEL',
        'detail': 'PRIVATE_SENTINEL',
    }, {
        'event': 'audio_generation_failed', 'stage': 'PRIVATE_SENTINEL',
        'exception_class': 'PRIVATE_SENTINEL', 'cause_class': 'PRIVATE_SENTINEL',
        'code': 'PRIVATE_SENTINEL', 'text_chars': 'PRIVATE_SENTINEL',
    }]})
    first, second = public['events']
    assert first == {'event': 'audio_generation_failed', 'at': 1., 'chunk_index': 1,
                     'stage': 'silero_tts', 'exception_class': 'RuntimeError', 'cause_class': 'ValueError',
                     'code': 'silero_empty_text', 'text_chars': 2}
    assert second == {'event': 'audio_generation_failed'}
    assert 'PRIVATE_SENTINEL' not in repr(public)
