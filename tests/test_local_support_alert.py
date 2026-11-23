"""Local help event/panel visibility is never adult delivery or acknowledgement."""
import json
import httpx
import pytest
from app.foundation.config import Settings
from app.foundation.stand import create_stand_app
from test_panel_session_actions import PanelStand


@pytest.mark.asyncio
@pytest.mark.parametrize('authorized', [False, True])
@pytest.mark.parametrize('up', [False, True])
async def test_help_button_and_confirmation_work_for_four_entry_combinations(tmp_path, authorized, up):
    h = PanelStand(tmp_path, identity=authorized)
    try:
        if authorized:
            await h.ready()
        await h.control('start')
        await h.menu()
        if up:
            await h.simulator.action({'action':'handset','state':'up'})
        await h.key(5)
        assert h.state.mode == 'wellbeing_check'
        assert 'support_alert' not in h.stand.panel_snapshot()['canonical_state']
        await h.key(7)
        assert h.state.mode == 'safety_stop'
        alert = h.stand.panel_snapshot()['canonical_state']['support_alert']
        assert alert == {'kind':'help_requested','message':'Нужна помощь взрослого рядом.',
                         'adult_acknowledged':False}
        assert h.state.handset == ('up' if up else 'down')
        # Even injected unrelated/private metadata must never enter the fixed alert.
        h.state.display_name = 'SYNTHETIC_PRIVATE_NAME'
        h.state.safety_severity = 'SYNTHETIC_PRIVATE_DETAIL'
        assert 'SYNTHETIC_' not in json.dumps(h.stand.panel_snapshot()['canonical_state']['support_alert'])
        await h.control('end')
        assert 'support_alert' not in h.stand.panel_snapshot()['canonical_state']
        if authorized:
            await h.ready()
            assert h.state.mode == 'profile_ready'
        else:
            await h.control('start')
        assert 'support_alert' not in h.stand.panel_snapshot()['canonical_state']
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_handset_only_mode_has_button_back_and_day_menu_needs_no_lift(tmp_path):
    h = PanelStand(tmp_path)
    try:
        await h.control('start')
        await h.menu()
        await h.key(6)
        assert h.state.mode == 'awaiting_handset'
        result = await h.key(7)
        assert h.state.mode == 'root_menu' and h.state.pending_mode is None, result
        await h.key(7)
        assert h.state.mode == 'schedule_weather' and h.state.handset == 'down'
        assert h.stand.panel_snapshot()['canonical_state']['scene_menu_descriptor']['scene'] == 'schedule_weather'
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_unrelated_hardware_stop_does_not_claim_a_child_requested_help(tmp_path):
    h = PanelStand(tmp_path)
    try:
        await h.control('start')
        await h.menu()
        h.state.mode = 'safety_stop'
        h.state.safety_severity = 'urgent'
        assert 'support_alert' not in h.stand.panel_snapshot()['canonical_state']
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_panel_state_requires_authentication_before_help_visibility():
    api = create_stand_app(Settings(profile='mock', stand_access_token='synthetic-help-access'))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url='http://test') as client:
            response = await client.get('/stand/state')
            assert response.status_code == 401
            assert 'support_alert' not in response.text
            response = await client.get('/stand/state', headers={'x-stand-token':'synthetic-help-access'})
            assert response.status_code == 200
    finally:
        await api.state.stand.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('last_button', [1, 7])
async def test_silent_help_question_tracks_no_answer_and_next_choice(tmp_path, last_button):
    h = PanelStand(tmp_path)
    try:
        await h.control('start')
        await h.menu()
        await h.key(5)
        assert h.stand.panel_snapshot()['canonical_state']['support_question'] == 'pain_or_discomfort'
        await h.key(1)
        assert h.state.support_question_kind == 'fear'
        assert h.stand.panel_snapshot()['canonical_state']['support_question'] == 'fear'
        await h.key(last_button)
        panel = h.stand.panel_snapshot()['canonical_state']
        assert 'support_question' not in panel
        if last_button == 7:
            assert panel['support_alert']['kind'] == 'help_requested'
        else:
            assert h.state.mode == 'root_menu' and 'support_alert' not in panel
    finally:
        await h.close()
