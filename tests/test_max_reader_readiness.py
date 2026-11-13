from types import SimpleNamespace
import pytest
from app.foundation.max_json import MaxJsonBoundary

class Client:
    event=None
    def poll(self):
        if isinstance(self.event,Exception): raise self.event
        return self.event
    def clear(self): pass

def boundary():
    app=SimpleNamespace(max_json=True,_max_client_boundary=None,canonical_runtime=SimpleNamespace(sessions={}))
    client=Client()
    return MaxJsonBoundary(app,client),client

@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["reader_permission_denied","reader_grab_failed","reader_identity_mismatch",
    "reader_interface_mismatch","reader_ambiguous","invalid_reader_path","invalid_reader_device",
    "unsupported_reader","capture_failed","device_disconnected","stopped","api_error"])
async def test_terminal_clear_never_preserves_reader_ready(reason):
    b,c=boundary()
    c.event={"kind":"reader_state","state":"connected"};await b.step()
    assert b._reader_ready
    c.event={"kind":"clear","reason_code":reason};await b.step()
    assert not b._reader_ready
    assert not b._healthy

@pytest.mark.asyncio
async def test_poll_failure_revokes_reader_readiness():
    b,c=boundary();b._reader_ready=True
    c.event=RuntimeError("simulated")
    await b.step()
    assert not b._reader_ready

@pytest.mark.asyncio
@pytest.mark.parametrize("reason",["scan_started","session_expired","no_personalization"])
async def test_normal_context_clear_does_not_imply_reader_disconnected(reason):
    b,c=boundary();b._reader_ready=True
    c.event={"kind":"clear","reason_code":reason};await b.step()
    assert b._reader_ready
