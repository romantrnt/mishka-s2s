import pytest
from app.foundation.stand import HardwareIntegrationStand
from app.foundation.config import Settings

@pytest.mark.asyncio
async def test_desynchronized_reader_reports_explicit_recovery_available():
    stand=HardwareIntegrationStand(Settings(profile='mock'),background_scheduler=False)
    try:
        # Construct mock I/O first; never open any physical device.
        stand.hardware_mode='physical'
        stand._on_hid_error({'kind':'input_desynchronized'},0)
        health=stand.health_snapshot()['hid']
        assert health['status']=='input_desynchronized'
        assert not health['physical_ready']
        assert not health['semantic_input_accepted']
        assert health['restart_allowed'] is True
    finally:
        stand.hardware_mode='simulator'
        await stand.aclose()
