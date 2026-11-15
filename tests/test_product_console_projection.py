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
