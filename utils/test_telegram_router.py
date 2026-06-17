"""Unit tests for repose.utils.telegram_router — Track 0 POL criteria 3-6."""

from unittest import mock

import pytest

from repose.utils import chronogram
from repose.utils.telegram_router import (
    AGENT_RATE_LIMITS,
    route_message,
    reset_rate_limits,
    reset_credentials,
    _set_telegram_config_override,
    _clear_telegram_config_override,
)

# Telegram credentials are injected via the in-process override hook. RPOSE-008
# removed env-var secret injection from bitwarden.py, so tests must use the
# established override pattern rather than environment-variable secret injection.
_TEST_TELEGRAM_CONFIG = {
    "bot_token": "test:fake_bot_token_123",
    "channels": {
        "critical": "-1001111111111",
        "informational": "-1002222222222",
    },
}


@pytest.fixture(autouse=True)
def setup_teardown():
    reset_rate_limits()
    reset_credentials()
    _set_telegram_config_override(_TEST_TELEGRAM_CONFIG)
    chronogram.clear_events()
    yield
    _clear_telegram_config_override()
    reset_rate_limits()
    reset_credentials()
    chronogram.clear_events()


# Test 1: Routes critical messages to critical channel
def test_routes_critical_to_critical_channel():
    with mock.patch("repose.utils.telegram_router._send_telegram_message", return_value=(True, "")):
        result = route_message(agent="event_monitor", message="CRITICAL: System outage", priority="critical", bypass_rate_limit=True)
    assert result["sent"] is True
    assert result["channel"] == "critical"
    assert result["rate_limited"] is False


# Test 2: Routes informational messages to informational channel
def test_routes_informational_to_informational_channel():
    with mock.patch("repose.utils.telegram_router._send_telegram_message", return_value=(True, "")):
        result = route_message(agent="intel_feed", message="Test message", priority="informational")
    assert result["sent"] is True
    assert result["channel"] == "informational"
    assert result["rate_limited"] is False


# Test 3: Enforces per-agent rate limits
def test_enforces_rate_limits():
    with mock.patch("repose.utils.telegram_router._send_telegram_message", return_value=(True, "")):
        morning_brief_limit = AGENT_RATE_LIMITS["morning_brief"]["per_minute"]
        for i in range(morning_brief_limit):
            result = route_message(agent="morning_brief", message=f"Msg {i}", priority="informational")
            assert result["sent"] is True
        result = route_message(agent="morning_brief", message="Rate limited", priority="informational")
        assert result["sent"] is False
        assert result["rate_limited"] is True
        # Other agent should still work
        result = route_message(agent="event_monitor", message="Unrelated", priority="informational")
        assert result["sent"] is True


# Test 4: Bypasses rate limit for priority=critical
def test_bypasses_rate_limit_for_critical():
    with mock.patch("repose.utils.telegram_router._send_telegram_message", return_value=(True, "")):
        morning_brief_limit = AGENT_RATE_LIMITS["morning_brief"]["per_minute"]
        for i in range(morning_brief_limit):
            route_message(agent="morning_brief", message=f"Fill {i}", priority="informational")
        result = route_message(agent="morning_brief", message="EMERGENCY", priority="critical", bypass_rate_limit=True)
        assert result["sent"] is True
        assert result["channel"] == "critical"
        assert result["rate_limited"] is False


# Test 5: Logs rate-limited messages to Chronogram
def test_logs_rate_limited_to_chronogram():
    chronogram.clear_events()
    with mock.patch("repose.utils.telegram_router._send_telegram_message", return_value=(True, "")):
        observer_limit = AGENT_RATE_LIMITS["observer"]["per_minute"]
        for i in range(observer_limit):
            route_message(agent="observer", message=f"Fill {i}", priority="informational")
        result = route_message(agent="observer", message="Should be rate-limited and logged", priority="informational")
        assert result["sent"] is False
        assert result["rate_limited"] is True
    events = chronogram.get_recent_events(namespace="system-events", agent="observer")
    rate_limited_events = [e for e in events if e.get("rate_limited")]
    assert len(rate_limited_events) >= 1
    assert rate_limited_events[0]["agent"] == "observer"
    assert rate_limited_events[0]["rate_limited"] is True


# Test 6: Does not raise on Telegram API failure (fail open)
def test_does_not_raise_on_telegram_failure():
    with mock.patch("repose.utils.telegram_router._send_telegram_message", return_value=(False, "Connection refused")):
        result = route_message(agent="intel_feed", message="Will fail", priority="informational")
    assert result["sent"] is False
    assert result["rate_limited"] is False
    assert "Telegram API failure" in result["reason"]
    events = chronogram.get_recent_events(namespace="system-events")
    error_events = [e for e in events if e.get("error")]
    assert len(error_events) >= 1


# Test 7: Returns correct dict shape
def test_returns_correct_dict_shape():
    with mock.patch("repose.utils.telegram_router._send_telegram_message", return_value=(True, "")):
        result = route_message(agent="event_monitor", message="Shape test", priority="informational")
    assert isinstance(result, dict)
    assert set(result.keys()) == {"sent", "channel", "rate_limited", "reason"}
    assert isinstance(result["sent"], bool)
    assert isinstance(result["channel"], str)
    assert isinstance(result["rate_limited"], bool)
    assert isinstance(result["reason"], str)


# Edge cases
def test_invalid_agent_returns_error():
    result = route_message(agent="nonexistent", message="test", priority="informational")
    assert result["sent"] is False
    assert "Invalid agent" in result["reason"]


def test_invalid_priority_returns_error():
    result = route_message(agent="intel_feed", message="test", priority="invalid")
    assert result["sent"] is False
    assert "Invalid priority" in result["reason"]
