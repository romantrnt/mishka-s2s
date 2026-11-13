"""JSON-only migration contracts: obsolete entry points fail closed."""
import asyncio
import os
from pathlib import Path

import pytest

from app.foundation import adapters, identity, domain
from app.foundation.api import build_application, create_app
from app.foundation.config import Settings, SettingsError
from app.foundation.events import parse_event, EventValidationError
from app.foundation.max_json import MaxJsonBoundary


@pytest.mark.parametrize('name', [
    'MISHKA_IDENTITY_PROVIDER', 'MISHKA_RFID_LOOKUP_TIMEOUT_S',
    'MISHKA_RFID_READY_TTL_S', 'MISHKA_RFID_DEBOUNCE_S', 'MISHKA_PROFILE_DB',
])
def test_retired_environment_fails_explicitly_without_echoing_value(monkeypatch, name):
    monkeypatch.setattr(os, 'environ', {name: 'PRIVATE_CONFIG_VALUE'})
    with pytest.raises(SettingsError) as error:
        Settings.from_env()
    assert name in str(error.value)
    assert 'PRIVATE_CONFIG_VALUE' not in str(error.value)


def test_new_history_configuration_names_only_session_history_storage(monkeypatch):
    monkeypatch.setattr(os, 'environ', {'MISHKA_HISTORY_DB': '/synthetic/history.sqlite3'})
    settings = Settings.from_env()
    assert settings.history_db == '/synthetic/history.sqlite3'
    assert 'history_db' not in settings.redacted_snapshot()
    assert not hasattr(settings, 'profile_db')


@pytest.mark.parametrize('mode', ['optional_rfid', 'clinical_rfid', 'allow_all'])
def test_old_modes_rejected(mode):
    with pytest.raises(SettingsError):
        Settings(identity_mode=mode).validate()


def test_clinical_json_and_real_hardware_remain_unaccepted():
    with pytest.raises(SettingsError, match='exhibition session policy'):
        Settings(identity_mode='max_json', runtime_mode='clinical').validate()
    for profile, hardware in [('production_candidate', 'physical'), ('mock', 'physical'), ('local_cpu', 'fake')]:
        with pytest.raises(SettingsError):
            Settings(identity_mode='max_json', profile=profile, hardware_mode=hardware).validate()


def test_deleted_uid_profile_lookup_implementations_are_absent():
    for name in ('SyntheticIdentityResolver', 'DisabledIdentityResolver', 'IdentityResolver', 'validate_uid'):
        assert not hasattr(identity, name)
    for name in ('MockProfileProvider', 'ExhibitionProfileProvider', 'SQLiteProfileProvider', 'CachedProfileProvider'):
        assert not hasattr(adapters, name)
    assert not hasattr(domain, 'RFIDReader')
    assert not hasattr(domain, 'ProfileProvider')
    app, *_ = build_application(settings=Settings(profile='mock', runtime_mode='clinical'), background_scheduler=False)
    assert not hasattr(app, 'profiles')
    assert not hasattr(app, 'identity_resolver')
    for name in ('_lookup_identity', '_identity_lookup_tasks', '_identity_provider_tasks'):
        assert not hasattr(app, name)
    with pytest.raises(ValueError, match='max_json_mode_required'):
        MaxJsonBoundary(app, object())
    paths = {route.path for route in create_app(application=app, include_test_routes=True).routes}
    assert '/mock/rfid' not in paths
    assert '/internal/profile-provider/failure' not in paths


@pytest.mark.parametrize('name', ['rfid', 'rfid_present', 'rfid_removed', 'RfidPresented', 'RfidRemoved',
                                 'IdentityLookupSucceeded', 'IdentityLookupFailed', 'IdentityLookupExpired'])
def test_retired_ingress_never_reaches_session_or_publisher(name):
    with pytest.raises(EventValidationError):
        parse_event({'type': name, 'tag_id': 'PRIVATE_UID'}, 'session', 1)
    async def run():
        app, *_ = build_application(settings=Settings(profile='mock'), background_scheduler=False)
        result = await app.receive_async(domain.Event(name, 'session', 1, 1.0, 'direct', {'tag_id': 'PRIVATE_UID'}))
        assert result['reason'] == 'unsupported_event'
        assert not app.canonical_runtime.sessions
        assert not app.sessions.items
        await app.shutdown_identity()
    asyncio.run(run())


@pytest.mark.parametrize("first_name", ["x" * 161, 42])
def test_rejected_profile_mapping_clears_client_and_all_local_context(first_name):
    class Client:
        def __init__(self):
            self.card = {
                "schema_version": 1, "record": {"state": "active"},
                "rfid": {"state": "active", "valid_until": "2099-01-01T00:00:00Z"},
                "visit": {"status": "open"}, "identity": {"first_name": first_name},
                "birth": {"age_years": 6, "birth_data_state": "complete"},
                "acquaintance": {"kind": "first"},
                "adaptation": {"wheelchair_access": False, "participation_mode": "seated"},
            }
        def start(self): return True
        def poll(self): return {"kind": "patient_ready"}
        def patient(self): return self.card
        def clear(self): self.card = None
        def close(self): self.clear(); return True
    async def run():
        app, *_ = build_application(settings=Settings(identity_mode="max_json"), background_scheduler=False)
        client = Client()
        boundary = MaxJsonBoundary(app, client)
        boundary.start(background=False)
        assert (await boundary.step())["reason"] == "invalid_client_profile"
        assert client.card is None and boundary.facts is None
        assert not app._canonical_profiles
        assert not app.identity_status()["identity_ready"]
        await app.shutdown_identity()
    asyncio.run(run())


@pytest.mark.parametrize("settings,client", [
    (Settings(identity_mode="max_json"), None),
    (Settings(identity_mode="guest_only"), object()),
])
def test_client_mode_preflight_happens_before_allocating_stand(monkeypatch, settings, client):
    import app.foundation.stand as module
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("Stand resources must not be allocated")
    monkeypatch.setattr(module, "HardwareIntegrationStand", forbidden)
    with pytest.raises(ValueError):
        module.create_stand_app(settings, phone_client=client)
    assert not calls
