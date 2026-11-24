"""An upgraded local signal must not look like a resolved request for help."""
from app.foundation.operator_monitor import OperatorMonitor


def test_help_upgrade_keeps_signal_active_without_false_resolution():
    monitor = OperatorMonitor()
    monitor.state({'mode': 'safety_stop', 'support_alert_kind': 'help_requested'})
    help_cursor = monitor.snapshot()['cursor']
    monitor.state({'mode': 'safety_stop', 'support_alert_kind': 'urgent_safety'})
    result = monitor.snapshot(after=help_cursor)
    assert result['alert']['kind'] == 'urgent_safety'
    assert result['alert']['adult_acknowledged'] is False
    assert result['alert']['delivery_confirmed'] is False
    assert [item['kind'] for item in result['items']] == ['alert_opened']
    assert result['items'][0]['data']['kind'] == 'urgent_safety'


def test_repeated_urgent_observation_does_not_duplicate_or_end_signal():
    monitor = OperatorMonitor()
    state = {'mode': 'safety_stop', 'support_alert_kind': 'urgent_safety'}
    monitor.state(state)
    before = monitor.snapshot()
    monitor.state(state)
    result = monitor.snapshot(after=before['cursor'])
    assert result['items'] == [] and result['alert'] == before['alert']


def test_actual_end_after_upgrade_records_one_unacknowledged_end():
    monitor = OperatorMonitor()
    monitor.state({'mode': 'safety_stop', 'support_alert_kind': 'help_requested'})
    monitor.state({'mode': 'safety_stop', 'support_alert_kind': 'urgent_safety'})
    monitor.state({'mode': 'session_closed'})
    result = monitor.snapshot()
    ended = [item for item in result['items'] if item['kind'] == 'alert_ended']
    assert len(ended) == 1
    assert ended[0]['data'] == {'kind': 'urgent_safety', 'adult_acknowledged': False}
    assert result['alert'] is None
