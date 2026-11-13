"""Explicit ownership of the unchanged, externally supplied Max demo client."""
import importlib
import os
from pathlib import Path
import socket
import stat
import sys
import threading
import zipfile


class MaxClientStartupError(RuntimeError):
    """Bounded startup diagnostic; no credentials or remote exception text."""


def _private_state(value):
    path = Path(value)
    if not path.is_absolute() or path.resolve() != path:
        raise MaxClientStartupError('max_demo_state_requires_absolute_private_directory')
    try:
        info = path.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise MaxClientStartupError('max_demo_state_permissions')
        for name in ('demo-local-only.json', 'demo.sqlite3', 'tls/server.key'):
            child = path / name
            info = child.stat()
            if child.resolve() != child or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise MaxClientStartupError('max_demo_state_permissions')
    except OSError:
        raise MaxClientStartupError('max_demo_state_unavailable') from None
    return path


def _load_demo_bundle(value):
    bundle = Path(value)
    if not bundle.is_absolute() or bundle.resolve() != bundle or not bundle.is_file():
        raise MaxClientStartupError('max_client_bundle_requires_absolute_zip')
    try:
        with zipfile.ZipFile(bundle) as archive:
            names = archive.namelist()
            required = {'phone_local_demo.py', 'phone_demo_database.py', 'phone_demo_tls.py',
                        'phone_ubuntu_client.py', 'ubuntu_z2.py'}
            if not required.issubset(names):
                raise MaxClientStartupError('max_client_bundle_missing_modules')
            roots = {n.split('/')[0].removesuffix('.py') for n in names if n.endswith('.py')} - {'__main__'}
        for name, module in tuple(sys.modules.items()):
            if name.split('.')[0] in roots:
                origin = str(getattr(module, '__file__', '') or '')
                if not origin.startswith(str(bundle) + '/'):
                    raise MaxClientStartupError('max_client_module_collision')
        if str(bundle) not in sys.path:
            sys.path.insert(0, str(bundle))
        demo = importlib.import_module('phone_local_demo')
        if not str(demo.__file__).startswith(str(bundle) + '/'):
            raise MaxClientStartupError('max_client_import_mismatch')
        return demo
    except MaxClientStartupError:
        raise
    except Exception:
        raise MaxClientStartupError('max_client_bundle_load_failed') from None


class _OwnedDemoClient:
    """Boundary owns this client, which also closes only its own demo server."""
    def __init__(self, client, server=None, thread=None):
        self.client, self.server, self.thread = client, server, thread

    def start(self): return self.client.start()
    def poll(self): return self.client.poll()
    def patient(self): return self.client.patient()
    def clear(self): return self.client.clear()

    def close(self):
        try:
            return self.client.close()
        finally:
            server, self.server = self.server, None
            if server is not None:
                server.shutdown()
                self.thread.join(5)
                server.server_close()
                if self.thread.is_alive():
                    raise MaxClientStartupError('max_demo_service_shutdown_incomplete')


def open_configured_client(settings):
    source = getattr(settings, 'max_client_source', 'disabled')
    if source == 'disabled':
        if settings.identity_mode == 'max_json':
            raise MaxClientStartupError('max_json_requires_configured_client')
        return None
    if source != 'local_demo' or settings.identity_mode != 'max_json' or not settings.max_synthetic_data:
        raise MaxClientStartupError('max_client_source_not_supported')
    state = _private_state(settings.max_demo_state)
    demo = _load_demo_bundle(settings.max_client_bundle)
    server = thread = None
    try:
        demo.load_state(state)
        # Never replace an existing listener, even if TLS/authentication fails.
        # Only an explicitly refused loopback connection permits starting our
        # own unchanged DemoServer. No state, CA or credential is generated.
        try:
            with socket.create_connection(('127.0.0.1', settings.max_demo_port), timeout=2):
                pass
        except ConnectionRefusedError:
            server = demo.DemoServer(state, port=settings.max_demo_port)
            thread = threading.Thread(target=server.serve_forever, name='max-local-demo', daemon=True)
            thread.start()
        client = demo.connect_demo(state, port=settings.max_demo_port)
        return _OwnedDemoClient(client, server, thread)
    except BaseException as error:
        if server is not None:
            if thread is not None and thread.is_alive():
                server.shutdown()
                thread.join(5)
            server.server_close()
        if not isinstance(error, Exception):
            raise
        raise MaxClientStartupError('max_demo_connection_failed_check_api_ca_and_state') from None


def build_configured_app(settings, factory):
    settings.validate()
    client = open_configured_client(settings)
    try:
        return factory(settings, phone_client=client)
    except BaseException:
        if client is not None:
            client.close()
        raise
