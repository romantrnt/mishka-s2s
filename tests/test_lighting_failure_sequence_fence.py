import asyncio
import os
import pytest
from test_scene_menu_runtime import Harness
from test_astra_native_lighting_incident import NativeTTY
from app.foundation.lighting import PhysicalLightingOutput,SerialLightingAdapter
from app.foundation.stand import UARTLEDController,HardwareIntegrationStand
from app.foundation.config import Settings
from types import SimpleNamespace

@pytest.mark.asyncio
async def test_actual_sequence_timer_cannot_relight_failed_output(monkeypatch):
    h=Harness();clock=[100.];h.app.monotonic_now=lambda:clock[0]
    tty=NativeTTY();serial=SerialLightingAdapter(tty.path);serial.open()
    stand=HardwareIntegrationStand(Settings(profile='mock'),background_scheduler=False)
    await stand.application.shutdown_identity()
    stand.application=h.app;stand.active_session_id=h.sid;stand.hardware_mode='physical'
    output=PhysicalLightingOutput(serial,auto_dark_recovery=True,on_fault=stand._on_lighting_fault)
    stand.lighting_output=output;stand.lighting_serial=serial
    h.app.led=UARTLEDController(SimpleNamespace(send=lambda x:None),output=output,monotonic_now=lambda:clock[0],background_scheduler=False)
    original_write=os.write;fd=serial.transport.fd;failed=[False]
    def fail_once(selected_fd,payload):
        if selected_fd==fd and not failed[0]:
            failed[0]=True;raise TimeoutError('synthetic single native failure')
        return original_write(selected_fd,payload)
    try:
        await h.button(1);await h.release(1);await h.button(1);await h.release(1)
        for _ in range(2):
            await h.button(6);await h.release(6);await h.button(1);await h.release(1)
        await h.button(2);await h.release(2);await h.complete()
        assert h.state.game_engine_state.phase=='demonstrate'
        scope=h.app._game_timer_scopes[h.sid]
        await asyncio.to_thread(output.flush);tty.receive()
        monkeypatch.setattr(os,'write',fail_once)
        output.set_element(13,1)
        with pytest.raises(ConnectionError):await asyncio.to_thread(output.flush)
        tty.receive();assert not any(tty.state.values())
        assert output.diagnostics()['generation_failed']
        if stand._lighting_fault_future is not None:
            await asyncio.wrap_future(stand._lighting_fault_future)
        assert h.state.mode=='safety_handoff'
        assert h.sid not in h.app._game_timer_scopes
        clock[0]=scope[-1]
        await h.app._fire_game_timer(h.sid,scope)
        # Timer-fired GAME_OUTPUT must not acquire a new physical-output owner.
        assert output.diagnostics()['generation_failed']
        tty.receive();assert not any(tty.state.values())
    finally:
        monkeypatch.setattr(os,'write',original_write)
        await stand.aclose();tty.close()
