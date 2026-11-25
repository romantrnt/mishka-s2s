"""Duplicate physical hook levels cannot erase an explicitly ended visit."""
from copy import deepcopy

import pytest

from test_panel_session_actions import PanelStand


@pytest.mark.asyncio
@pytest.mark.parametrize('handset_up',[False,True])
async def test_duplicate_hook_after_panel_end_preserves_end_marker_and_fresh_entry(tmp_path,handset_up):
    h=PanelStand(tmp_path,identity=handset_up)
    try:
        if handset_up:
            await h.ready()
            await h.simulator.action({'action':'handset','state':'up'})
        await h.control('start');await h.control('end')
        assert h.state.mode=='session_closed' and h.state.substate=='soft_end'
        before=deepcopy(h.state.snapshot())
        revision=h.app._session_revisions.get('stand')
        lighting=deepcopy(h.stand.lighting_output.snapshot())
        await h.simulator.action({'action':'handset','state':'up' if handset_up else 'down'})
        assert h.state.snapshot()==before
        assert h.app._session_revisions.get('stand')==revision
        assert h.stand.lighting_output.snapshot()==lighting
        if handset_up:
            assert (await h.ready())['reason']=='profile_ready'
            assert h.state.mode=='profile_ready' and h.state.profile_status=='identified'
        else:
            await h.simulator.action({'action':'handset','state':'up'})
            assert h.state.mode=='voice_welcome' and h.state.interaction_open
    finally:
        await h.close()
