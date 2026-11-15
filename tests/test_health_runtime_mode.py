import pytest
from app.foundation.config import Settings
from app.foundation.stand import HardwareIntegrationStand

@pytest.mark.parametrize('mode', ['clinical', 'exhibition'])
def test_health_reports_actual_runtime_mode_not_a_fixed_exhibition_label(mode):
    stand = HardwareIntegrationStand(Settings(profile='mock',runtime_mode=mode), background_scheduler=False)
    try:
        assert stand.health_snapshot()['mode'] == mode
    finally:
        stand.close()
