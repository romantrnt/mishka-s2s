from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from app.foundation import max_client_runtime as r


def settings(**kw):
    v=dict(identity_mode='max_json',max_client_source='local_demo',max_synthetic_data=True,
           max_demo_state='/demo',max_client_bundle='/bundle.zip',max_demo_port=19443)
    v.update(kw)
    return SimpleNamespace(**v)


def test_guest_does_not_import(monkeypatch):
    monkeypatch.setattr(r,'_load_demo_bundle',lambda _: pytest.fail('unexpected import'))
    assert r.open_configured_client(settings(identity_mode='guest_only',max_client_source='disabled')) is None


@pytest.mark.parametrize('changes',[dict(max_client_source='external'),dict(max_synthetic_data=False),dict(identity_mode='guest_only'),dict(max_client_source='disabled')])
def test_reject_source(changes):
    with pytest.raises(r.MaxClientStartupError):r.open_configured_client(settings(**changes))


def setup(monkeypatch, refused=False):
    demo=Mock(); monkeypatch.setattr(r,'_private_state',lambda _: '/verified')
    monkeypatch.setattr(r,'_load_demo_bundle',lambda _:demo)
    connection=Mock();connection.__enter__=Mock();connection.__exit__=Mock()
    probe=Mock(return_value=connection,side_effect=ConnectionRefusedError() if refused else None)
    monkeypatch.setattr(r.socket,'create_connection',probe)
    return demo


def test_existing_service_not_owned(monkeypatch):
    demo=setup(monkeypatch)
    client=r.open_configured_client(settings())
    demo.load_state.assert_called_once_with('/verified')
    demo.connect_demo.assert_called_once_with('/verified',port=19443)
    client.close();demo.DemoServer.assert_not_called()


def test_owned_server_stopped(monkeypatch):
    demo=setup(monkeypatch,True)
    client=r.open_configured_client(settings())
    client.close()
    demo.DemoServer.return_value.shutdown.assert_called_once()
    demo.DemoServer.return_value.server_close.assert_called_once()


def test_tls_error_does_not_replace_listener(monkeypatch):
    demo=setup(monkeypatch);demo.connect_demo.side_effect=ValueError('SECRET')
    with pytest.raises(r.MaxClientStartupError) as exc:r.open_configured_client(settings())
    assert 'SECRET' not in str(exc.value)
    demo.DemoServer.assert_not_called()


def test_construction_failure_closes(monkeypatch):
    client=Mock();monkeypatch.setattr(r,'open_configured_client',lambda _:client)
    s=settings();s.validate=Mock()
    def fail(*a,**kw):raise ValueError()
    with pytest.raises(ValueError):r.build_configured_app(s,fail)
    client.close.assert_called_once()


def test_private_state(tmp_path):
    tmp_path.chmod(0o700);(tmp_path/'tls').mkdir()
    for name in ('demo-local-only.json','demo.sqlite3','tls/server.key'):
        f=tmp_path/name;f.write_text('x');f.chmod(0o600)
    assert r._private_state(str(tmp_path))==tmp_path
    (tmp_path/'demo-local-only.json').chmod(0o644)
    with pytest.raises(r.MaxClientStartupError):r._private_state(str(tmp_path))


def test_owned_server_cleanup_on_connect_failure(monkeypatch):
    demo=setup(monkeypatch,True)
    demo.connect_demo.side_effect=ValueError('private remote details')
    with pytest.raises(r.MaxClientStartupError):r.open_configured_client(settings())
    demo.DemoServer.return_value.server_close.assert_called_once()
    demo.initialize.assert_not_called()


def test_owned_server_cleanup_preserves_interrupt(monkeypatch):
    demo=setup(monkeypatch,True)
    demo.connect_demo.side_effect=KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):r.open_configured_client(settings())
    demo.DemoServer.return_value.server_close.assert_called_once()
    demo.initialize.assert_not_called()
