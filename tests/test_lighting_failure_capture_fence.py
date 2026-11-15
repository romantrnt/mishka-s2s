"""A current serial-output fault must fence physical audio owners."""
import asyncio
import os
import pytest
from app.foundation.config import Settings
from app.foundation.stand import HardwareIntegrationStand
from app.foundation.lighting import PhysicalLightingOutput, SerialLightingAdapter

@pytest.mark.asyncio
async def test_native_serial_failure_revokes_existing_physical_capture_owner(monkeypatch):
    stand=HardwareIntegrationStand(Settings(profile='mock',runtime_mode='clinical'),background_scheduler=False)
    stand._admits_physical_users=lambda:True
    master,slave=os.openpty()
    serial=SerialLightingAdapter(os.ttyname(slave));serial.open()
    output=PhysicalLightingOutput(serial)
    old_output=stand.lighting_output
    try:
        await stand.inject_physical({'type':'SpaceReleased'})
        await stand.inject_physical({'type':'ButtonPressed','button_id':6})
        assert stand.application.last_canonical_state['mode']=='bear_dialogue'
        assert stand._headless_audio_accepting()
        guard=stand._audio_owner_guard(stand.active_session_id,physical=True)
        assert guard()
        stand.hardware_mode='physical';stand.lighting_output=output;stand.lighting_serial=serial
        fd=serial.transport.fd;original_write=os.write
        def fail(selected_fd,payload):
            if selected_fd==fd:raise TimeoutError('native fault probe')
            return original_write(selected_fd,payload)
        monkeypatch.setattr(os,'write',fail)
        output.set_element(10,1)
        with pytest.raises(ConnectionError):await asyncio.to_thread(output.flush)
        assert not output.diagnostics()['output_ready']
        assert not stand._headless_audio_accepting()
        assert not guard()
    finally:
        monkeypatch.undo()
        stand.hardware_mode='simulator';stand.lighting_output=old_output;stand.lighting_serial=None
        output.close();serial.close();os.close(master);os.close(slave)
        await stand.aclose()
