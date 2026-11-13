from dataclasses import replace
import pytest
from app.foundation.config import Settings, SettingsError

def live():
    return Settings(profile="local_gpu",hardware_mode="physical",runtime_mode="exhibition",
                    identity_mode="max_json",max_client_source="local_demo",
                    max_client_bundle="/opt/max/client.zip",max_demo_state="/var/lib/mishka/demo",
                    max_synthetic_data=True,allow_degraded_hardware=False,stand_access_token="test-only",
                    hid_device="/dev/input/by-id/arduino",lighting_device="/dev/serial/by-id/arduino")

def test_explicit_live_demo_and_injected_mock_are_supported():
    assert live().validate().max_synthetic_data
    assert Settings(identity_mode="max_json").validate().profile=="mock"

@pytest.mark.parametrize("change", [
    {"max_synthetic_data":False},{"max_client_source":"remote"},{"profile":"production_candidate"},
    {"profile":"production"},{"runtime_mode":"clinical"},{"max_client_bundle":"relative.zip"},
    {"max_demo_state":""},{"max_demo_port":True},{"max_demo_port":443},
    {"max_demo_port":65536},{"allow_degraded_hardware":True},{"stand_access_token":""},{"allow_query_token":True},{"hid_device":""},{"lighting_device":""},{"identity_mode":"guest_only"},{"max_client_source":"disabled"},
])
def test_live_demo_cannot_silently_become_production(change):
    with pytest.raises(SettingsError): replace(live(),**change).validate()

def test_live_identity_without_source_is_rejected():
    with pytest.raises(SettingsError):
        Settings(profile="local_gpu",hardware_mode="physical",identity_mode="max_json").validate()

def test_env_requires_explicit_synthetic_flag(monkeypatch):
    monkeypatch.setenv("MISHKA_PROFILE","local_gpu")
    monkeypatch.setenv("MISHKA_HARDWARE_MODE","physical")
    monkeypatch.setenv("MISHKA_IDENTITY_MODE","max_json")
    monkeypatch.setenv("MISHKA_MAX_CLIENT_SOURCE","local_demo")
    monkeypatch.setenv("MISHKA_MAX_CLIENT_BUNDLE","/opt/max/client.zip")
    monkeypatch.setenv("MISHKA_MAX_DEMO_STATE","/var/lib/mishka/demo")
    with pytest.raises(SettingsError): Settings.from_env()
    monkeypatch.setenv("MISHKA_MAX_SYNTHETIC_DATA","true")
    monkeypatch.setenv("MISHKA_ALLOW_DEGRADED_HARDWARE","false")
    monkeypatch.setenv("MISHKA_HID_DEVICE","/dev/input/by-id/arduino")
    monkeypatch.setenv("MISHKA_LIGHTING_DEVICE","/dev/serial/by-id/arduino")
    monkeypatch.setenv("STAND_ACCESS_TOKEN","test-only")
    assert Settings.from_env().max_client_source=="local_demo"
