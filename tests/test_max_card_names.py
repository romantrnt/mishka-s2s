"""DATA14 names never invalidate an otherwise valid card due to speech limits."""
from datetime import datetime, timezone
import pytest
from app.foundation.max_json import _map_card
from app.foundation.identity import IdentityValidationError


def card(name):
    return {"schema_version": 1, "record": {"state": "active"},
            "rfid": {"state": "active", "valid_until": "2030-01-01T00:00:00Z"},
            "visit": {"status": "open"}, "identity": {"first_name": name},
            "birth": {"age_years": 9, "birth_data_state": "complete"},
            "acquaintance": {"kind": "first"},
            "adaptation": {"wheelchair_access": False, "participation_mode": "standing"}}


@pytest.mark.parametrize("length", [1, 80, 81, 160])
def test_valid_name_preserves_card_without_truncating_long_name(length):
    name = "А" * length
    profile, deadline, facts = _map_card(card(name), "test-context", datetime(2029, 1, 1, tzinfo=timezone.utc), 0)
    assert profile["age_years"] == 9 and profile["profile_ref"] == "test-context"
    assert deadline > 0 and facts["acquaintance"] == "first"
    if length <= 80:
        assert profile["display_name"] == name
    else:
        assert "display_name" not in profile
        assert name not in repr(profile)


@pytest.mark.parametrize("name", ["А"*161, "", " ", 42, "А"*81+"\n", "А"*81+"\x7f", "А"*81+"\ud800"])
def test_invalid_names_are_not_silently_dropped(name):
    with pytest.raises(IdentityValidationError):
        _map_card(card(name), "test-context", datetime(2029, 1, 1, tzinfo=timezone.utc), 0)


def test_absent_name_keeps_profile():
    profile, _, _ = _map_card(card(None), "test-context", datetime(2029, 1, 1, tzinfo=timezone.utc), 0)
    assert "display_name" not in profile and profile["age_years"] == 9
