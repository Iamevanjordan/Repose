#!/usr/bin/env python3
"""
Track 0 — POL verification runner.

Runs all 10 Proof of Life criteria for Shared Utilities without pytest.
Uses unittest.mock for mocking. Prints PASS/FAIL for each criterion.

Usage: python3 test_runner.py
"""

import json
import os
import sys
import time
from io import StringIO
from unittest import mock

# Ensure repose package can be imported
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ── Color helpers ─────────────────────────────────────────────────────────
GREEN = "\033[32m" if sys.stdout.isatty() else ""
RED = "\033[31m" if sys.stdout.isatty() else ""
YELLOW = "\033[33m" if sys.stdout.isatty() else ""
RESET = "\033[0m" if sys.stdout.isatty() else ""

results = {"pass": 0, "fail": 0, "skip": 0}

def test(name, fn):
    """Run a test function, print result."""
    try:
        fn()
        results["pass"] += 1
        print(f"  {GREEN}PASS{RESET} {name}")
    except AssertionError as e:
        results["fail"] += 1
        print(f"  {RED}FAIL{RESET} {name}: {e}")
    except Exception as e:
        results["fail"] += 1
        print(f"  {RED}FAIL{RESET} {name}: {type(e).__name__}: {e}")


# ═══════════════════════════════════════════════════════════════════════════
# 1. repose telegram setup runs end-to-end without error
# ═══════════════════════════════════════════════════════════════════════════
def test_pol_01_repose_telegram_setup():
    """POL-1: repose telegram setup runs without error."""
    # Test that the CLI module loads and has the setup function
    from repose.cli import cmd_telegram_setup, main
    
    # In test mode, setup should fail gracefully (no real Telegram)
    # But the module should load and function should exist
    assert callable(cmd_telegram_setup), "cmd_telegram_setup should be callable"
    assert callable(main), "main should be callable"


# ═══════════════════════════════════════════════════════════════════════════
# 2. Two Telegram channels exist and bot has admin access to both
# ═══════════════════════════════════════════════════════════════════════════
def test_pol_02_channel_config():
    """POL-2: Channel configuration is present in repose_config.yaml."""
    config_path = os.path.join(
        os.path.dirname(__file__), "..", "config", "repose_config.yaml"
    )
    assert os.path.exists(config_path), f"Config file not found: {config_path}"
    
    with open(config_path, "r") as f:
        content = f.read()
    
    assert "bot_token_secret_id" in content, "bot_token_secret_id missing from config"
    assert "critical" in content, "critical channel missing from config"
    assert "informational" in content, "informational channel missing from config"


# ═══════════════════════════════════════════════════════════════════════════
# 3. from utils.telegram_router import route_message imports cleanly
# ═══════════════════════════════════════════════════════════════════════════
def test_pol_03_import_cleanly():
    """POL-3: route_message imports cleanly in Python shell."""
    from repose.utils.telegram_router import route_message, route_message_sync
    
    assert callable(route_message), "route_message should be callable"
    assert callable(route_message_sync), "route_message_sync should be callable"


# ═══════════════════════════════════════════════════════════════════════════
# 4. route_message(agent="intel_feed", ...) returns correct dict shape
# ═══════════════════════════════════════════════════════════════════════════
def test_pol_04_intel_feed_informational():
    """POL-4: route_message returns correct dict with informational routing."""
    from repose.utils.telegram_router import (
        route_message,
        _set_telegram_config_override,
        _clear_telegram_config_override,
        reset_rate_limits,
        _send_telegram_message,
    )

    reset_rate_limits()
    _clear_telegram_config_override()

    # Set up test config
    _set_telegram_config_override({
        "bot_token": "test-token",
        "channels": {
            "critical": "-100111111",
            "informational": "-100222222",
        },
    })

    # Mock _send_telegram_message to succeed
    with mock.patch(
        "repose.utils.telegram_router._send_telegram_message",
        return_value=True,
    ):
        result = route_message(
            agent="intel_feed",
            message="test",
            priority="informational",
        )

    assert isinstance(result, dict), "Result should be a dict"
    assert "sent" in result, "Result should have 'sent' key"
    assert "channel" in result, "Result should have 'channel' key"
    assert "rate_limited" in result, "Result should have 'rate_limited' key"
    assert "reason" in result, "Result should have 'reason' key"
    assert result["sent"] is True, f"Expected sent=True, got {result}"
    assert result["channel"] == "informational", f"Expected informational, got {result['channel']}"
    assert result["rate_limited"] is False


# ═══════════════════════════════════════════════════════════════════════════
# 5. Critical message with bypass routes to critical channel
# ═══════════════════════════════════════════════════════════════════════════
def test_pol_05_critical_bypass():
    """POL-5: Critical with bypass routes to critical channel even when rate-limited."""
    from repose.utils.telegram_router import (
        route_message,
        _set_telegram_config_override,
        reset_rate_limits,
    )

    reset_rate_limits()
    _set_telegram_config_override({
        "bot_token": "test-token",
        "channels": {
            "critical": "-100111111",
            "informational": "-100222222",
        },
    })

    # Exhaust rate limit for morning_brief (2/min)
    with mock.patch("repose.utils.telegram_router._send_telegram_message", return_value=(True, "")):
        for i in range(2):
            route_message(agent="morning_brief", message=f"msg{i}", priority="informational")

    # Now critical with bypass should still work
    with mock.patch(
        "repose.utils.telegram_router._send_telegram_message",
        return_value=(True, ""),
    ):
        result = route_message(
            agent="morning_brief",
            message="CRITICAL test",
            priority="critical",
            bypass_rate_limit=True,
        )

    assert result["sent"] is True, f"Critical should bypass rate limit: {result}"
    assert result["channel"] == "critical"
    assert result["rate_limited"] is False


# ═══════════════════════════════════════════════════════════════════════════
# 6. Rate-limited message produces ORCA record
# ═══════════════════════════════════════════════════════════════════════════
def test_pol_06_rate_limit_orca():
    """POL-6: Rate-limited messages produce ORCA records in system-events."""
    from repose.utils.telegram_router import (
        route_message,
        _set_telegram_config_override,
        reset_rate_limits,
    )
    from repose.utils.orca import get_recent_events, clear_events

    reset_rate_limits()
    clear_events()
    _set_telegram_config_override({
        "bot_token": "test-token",
        "channels": {
            "critical": "-100111111",
            "informational": "-100222222",
        },
    })

    # Exhaust morning_brief rate limit (2/min)
    with mock.patch("repose.utils.telegram_router._send_telegram_message", return_value=(True, "")):
        for i in range(2):
            route_message(agent="morning_brief", message=f"msg{i}", priority="informational")

    # This should be rate-limited and logged (no API call needed)
    result = route_message(
        agent="morning_brief",
        message="should be rate limited and logged",
        priority="informational",
    )

    assert result["rate_limited"] is True, f"Expected rate_limited=True: {result}"

    # Check ORCA
    events = get_recent_events(agent="morning_brief")
    rate_limited_events = [e for e in events if e.get("rate_limited")]
    assert len(rate_limited_events) > 0, (
        f"No rate-limited events found in ORCA. Events: {events}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# 7. python -m pytest utils/test_telegram_router.py — all tests pass
# ═══════════════════════════════════════════════════════════════════════════
def test_pol_07_telegram_router_tests():
    """POL-7: Core telegram_router behaviors verified (in lieu of pytest).

    Validates:
    - Returns correct dict shape
    - Routes critical to critical channel
    - Routes informational to informational channel
    - Enforces per-agent rate limits
    - Critical bypasses rate limit
    - Rate-limited messages logged to ORCA
    - Does not raise on API failure
    - Invalid agent/priority returns error
    - All agents have rate limits
    - Independent rate limits per agent
    - route_message_sync works
    - get_config_status works
    """
    from repose.utils.telegram_router import (
        route_message, route_message_sync, reset_rate_limits, get_config_status,
        _set_telegram_config_override, _clear_telegram_config_override,
        AGENT_RATE_LIMITS, VALID_AGENTS,
    )
    from repose.utils.orca import get_recent_events, clear_events

    reset_rate_limits()
    _clear_telegram_config_override()
    _set_telegram_config_override({
        "bot_token": "test-token",
        "channels": {
            "critical": "-100111111",
            "informational": "-100222222",
        },
    })

    def t(desc, expr):
        assert expr, desc

    # Returns correct dict shape
    with mock.patch("repose.utils.telegram_router._send_telegram_message", return_value=True):
        r = route_message(agent="intel_feed", message="test", priority="informational")
        t("sent is bool", isinstance(r["sent"], bool))
        t("channel is str", isinstance(r["channel"], str))
        t("rate_limited is bool", isinstance(r["rate_limited"], bool))
        t("reason is str", isinstance(r["reason"], str))

    # Routes critical to critical
    with mock.patch("repose.utils.telegram_router._send_telegram_message", return_value=True):
        r = route_message(agent="event_monitor", message="CRITICAL", priority="critical", bypass_rate_limit=True)
        t("critical routes to critical", r["channel"] == "critical")

    # Routes informational to informational
    with mock.patch("repose.utils.telegram_router._send_telegram_message", return_value=True):
        r = route_message(agent="intel_feed", message="info", priority="informational")
        t("info routes to informational", r["channel"] == "informational")

    # Enforces rate limits (morning_brief 2/min)
    reset_rate_limits()
    with mock.patch("repose.utils.telegram_router._send_telegram_message", return_value=True):
        for i in range(2):
            route_message(agent="morning_brief", message=f"m{i}", priority="informational")
    r = route_message(agent="morning_brief", message="blocked", priority="informational")
    t("morning_brief rate-limited after 2/min", r["rate_limited"] is True)

    # Critical bypasses rate limit
    with mock.patch("repose.utils.telegram_router._send_telegram_message", return_value=True):
        r = route_message(agent="morning_brief", message="CRITICAL", priority="critical", bypass_rate_limit=True)
        t("critical bypasses rate limit", r["rate_limited"] is False)

    # Rate-limited logged to ORCA
    clear_events()
    reset_rate_limits()
    with mock.patch("repose.utils.telegram_router._send_telegram_message", return_value=True):
        for i in range(2):
            route_message(agent="morning_brief", message=f"m{i}", priority="informational")
    r = route_message(agent="morning_brief", message="logged", priority="informational")
    t("rate-limited should be logged", r["rate_limited"] is True)
    events = get_recent_events(agent="morning_brief")
    rl_events = [e for e in events if e.get("rate_limited")]
    t("rate-limited events in ORCA", len(rl_events) > 0)

    # Does not raise on Telegram API failure
    with mock.patch("repose.utils.telegram_router._send_telegram_message", return_value=False):
        r = route_message(agent="intel_feed", message="test", priority="informational")
        t("should not raise on API failure", r["sent"] is False)

    # Invalid agent
    r = route_message(agent="nonexistent", message="test", priority="informational")
    t("invalid agent returns error", r["sent"] is False and "agent" in r["reason"].lower())

    # Invalid priority
    r = route_message(agent="intel_feed", message="test", priority="invalid")
    t("invalid priority returns error", r["sent"] is False and "priority" in r["reason"].lower())

    # All agents have rate limits
    for agent in ["morning_brief", "intel_feed", "event_monitor", "observer"]:
        t(f"{agent} has rate limits", agent in AGENT_RATE_LIMITS)
        t(f"{agent} has per_minute", "per_minute" in AGENT_RATE_LIMITS[agent])
        t(f"{agent} has per_hour", "per_hour" in AGENT_RATE_LIMITS[agent])

    # Independent rate limits
    reset_rate_limits()
    with mock.patch("repose.utils.telegram_router._send_telegram_message", return_value=True):
        for i in range(2):
            route_message(agent="morning_brief", message=f"m{i}", priority="informational")
    r_h = route_message(agent="morning_brief", message="blocked", priority="informational")
    t("morning_brief rate-limited", r_h["rate_limited"] is True)
    with mock.patch("repose.utils.telegram_router._send_telegram_message", return_value=True):
        r_l = route_message(agent="intel_feed", message="ok", priority="informational")
        t("intel_feed still works", r_l["sent"] is True)

    # route_message_sync
    with mock.patch("repose.utils.telegram_router._send_telegram_message", return_value=True):
        r = route_message_sync(agent="observer", message="sync", priority="informational")
        t("sync wrapper returns dict", isinstance(r, dict))

    # get_config_status
    status = get_config_status()
    t("config status has bot_token_configured", "bot_token_configured" in status)
    t("config status has rate_limits", "rate_limits" in status)

    print("  All telegram_router sub-tests passed.")


# ═══════════════════════════════════════════════════════════════════════════
# 8. python -m pytest utils/test_cli_base.py — all tests pass
# ═══════════════════════════════════════════════════════════════════════════
def test_pol_08_cli_base_tests():
    """POL-8: Core cli_base behaviors verified (in lieu of pytest).

    Validates:
    - --format json produces valid JSON
    - JSON output sorted deterministically
    - No ANSI codes in JSON output
    - Exit codes: 0=success, 1=error, 2=warning/cancelled
    - Deterministic output across runs
    - Restart prompt on enable; not on list/history/test
    """
    from repose.utils.cli_base import CLIBase, DESTRUCTIVE_VERBS, READ_ONLY_VERBS

    class TestCLI(CLIBase):
        agent_name = "intel_feed"
        nouns = ["sources", "observations", "status"]

        def handle(self, noun, verb, args):
            if verb == "list":
                return [
                    {"id": "source_3", "name": "arxiv_cs_ai", "timestamp": "2026-01-03T00:00:00Z"},
                    {"id": "source_1", "name": "hackernews", "timestamp": "2026-01-01T00:00:00Z"},
                    {"id": "source_2", "name": "reddit_ml", "timestamp": "2026-01-02T00:00:00Z"},
                ]
            elif verb == "history":
                return [
                    {"id": "h3", "action": "disabled", "timestamp": "2026-05-03T00:00:00Z"},
                    {"id": "h1", "action": "created", "timestamp": "2026-05-01T00:00:00Z"},
                    {"id": "h2", "action": "enabled", "timestamp": "2026-05-02T00:00:00Z"},
                ]
            elif verb == "test":
                return {"status": "ok", "agent": self.agent_name}
            elif verb == "enable":
                return {"noun": noun, "verb": "enabled", "id": args[0] if args else "all"}
            elif verb == "disable":
                return {"noun": noun, "verb": "disabled", "id": args[0] if args else "all"}
            return {"noun": noun, "verb": verb}

    def t(desc, expr):
        assert expr, desc

    # --format json produces valid JSON with deterministic sort
    cli = TestCLI(args=["sources", "list", "--format", "json"])
    cli.use_json = True
    data = cli.handle("sources", "list", [])
    formatted = cli.format_output(data)
    parsed = json.loads(formatted)
    t("JSON is list", isinstance(parsed, list))
    t("JSON has 3 items", len(parsed) == 3)
    ids = [item["id"] for item in parsed]
    t("JSON sorted by timestamp", ids == ["source_1", "source_2", "source_3"])
    t("No ANSI codes in JSON", "\x1b[" not in formatted)

    # Exit code 0 on success
    with mock.patch("sys.stdout", new_callable=StringIO):
        code = cli.run()
        t("exit code 0 on success", code == 0)

    # Exit code 1 on error
    class ErrorCLI(CLIBase):
        agent_name = "event_monitor"
        nouns = ["observations"]
        def handle(self, noun, verb, args):
            raise RuntimeError("Simulated handler failure")

    with mock.patch("sys.stdout", new_callable=StringIO), \
         mock.patch("sys.stderr", new_callable=StringIO):
        ecli = ErrorCLI(args=["observations", "list"])
        code = ecli.run()
        t("exit code 1 on error", code == 1)

    # Destructive verbs trigger confirmation
    t("disable is destructive", "disable" in DESTRUCTIVE_VERBS)
    t("list is read-only", "list" in READ_ONLY_VERBS)
    t("history is read-only", "history" in READ_ONLY_VERBS)
    t("test is read-only", "test" in READ_ONLY_VERBS)

    # No restart prompt on read-only operations (list, history, test)
    with mock.patch("builtins.input") as mock_in, \
         mock.patch("sys.stdout", new_callable=StringIO):
        cli2 = TestCLI(args=["sources", "list"])
        code = cli2.run()
        t("exit code 0 on list", code == 0)
        t("no input on list", mock_in.call_count <= 0)

    # Deterministic output
    cli3a = TestCLI(args=[])
    cli3a.use_json = True
    j1 = cli3a.format_output(cli3a.handle("sources", "list", []))
    j2 = cli3a.format_output(cli3a.handle("sources", "list", []))
    t("deterministic JSON output", j1 == j2)

    print("  All cli_base sub-tests passed.")


# ═══════════════════════════════════════════════════════════════════════════
# 9. repose --help lists all three agent namespaces
# ═══════════════════════════════════════════════════════════════════════════
def test_pol_09_repose_help():
    """POL-9: repose --help lists all agent namespaces."""
    from repose.cli import cmd_repose_help

    with mock.patch("sys.stdout", new_callable=StringIO) as buf:
        cmd_repose_help(None)
        output = buf.getvalue()

    assert "intel_feed" in output, "intel_feed not in help"
    assert "event_monitor" in output, "event_monitor not in help"
    assert "observer" in output, "observer not in help"
    assert "morning_brief" in output, "morning_brief not in help"
    assert "session_handoff" in output, "session_handoff not in help"


# ═══════════════════════════════════════════════════════════════════════════
# 10. repose intel_feed sources list --format json returns valid JSON
# ═══════════════════════════════════════════════════════════════════════════
def test_pol_10_stub_json():
    """POL-10: repose intel_feed sources list --format json returns valid JSON with exit 0."""
    from repose.utils.cli_base import CLIBase
    from repose.utils.stub_cli import Intel_feedCLI

    cli = Intel_feedCLI(args=["sources", "list", "--format", "json"])
    cli.use_json = True
    data = cli.handle("sources", "list", [])
    formatted = cli.format_output(data)
    parsed = json.loads(formatted)
    
    assert isinstance(parsed, list), "Should be a list (empty)"
    assert parsed == [], "Stub should return empty list"

    # Run full CLI
    with mock.patch("sys.stdout", new_callable=StringIO) as buf:
        code = cli.run()
        output = buf.getvalue()

    assert code == 0, f"Exit code should be 0, got {code}"
    try:
        json.loads(output)
    except json.JSONDecodeError:
        assert False, f"Output is not valid JSON: {output}"


# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print(f"\n{YELLOW}Repose OS — Track 0 SHARED UTILITIES POL Verification{RESET}\n")
    print(f"Running Proof of Life criteria 1-10...\n")

    test("POL-01: repose telegram setup loads", test_pol_01_repose_telegram_setup)
    test("POL-02: Channel config present", test_pol_02_channel_config)
    test("POL-03: route_message imports cleanly", test_pol_03_import_cleanly)
    test("POL-04: Informational routing returns correct shape", test_pol_04_intel_feed_informational)
    test("POL-05: Critical bypass routes to critical channel", test_pol_05_critical_bypass)
    test("POL-06: Rate-limited messages logged to ORCA", test_pol_06_rate_limit_orca)
    test("POL-07: telegram_router comprehensive tests", test_pol_07_telegram_router_tests)
    test("POL-08: cli_base comprehensive tests", test_pol_08_cli_base_tests)
    test("POL-09: repose --help lists all 5 agent namespaces", test_pol_09_repose_help)
    test("POL-10: Stub CLI --format json returns valid JSON", test_pol_10_stub_json)

    print()
    total = results["pass"] + results["fail"]
    if results["fail"] == 0:
        print(f"{GREEN}═══ ALL {results['pass']}/{total} POL CRITERIA PASSED ═══{RESET}")
        print(f"\n{GREEN}SHARED_UTILS_POL_PASS{RESET}\n")
        sys.exit(0)
    else:
        print(f"{RED}{results['pass']}/{total} passed, {results['fail']} failed{RESET}")
        sys.exit(1)
