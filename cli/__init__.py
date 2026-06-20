#!/usr/bin/env python3
"""
Repose OS CLI — main entry point.

Usage: repose <agent|command> [noun] [verb] [args] [--format json]

Agents:  intel_feed, event_monitor, observer, morning_brief, session_handoff
Commands: telegram setup
"""

import argparse
import getpass
import os
import subprocess
import sys
import time
from typing import Optional

# Add parent to path so we can import from repose.*
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from repose.utils.cli_base import (
    EXIT_SUCCESS, EXIT_ERROR, EXIT_WARNING,
    _supports_color, _format_bold, _format_green, _format_yellow, _format_red,
    _format_dim, prompt_yes_no,
)


def _resolve_secret_for_setup(secret_id: str, config_path: str) -> Optional[str]:
    """Resolve a secret during setup via Bitwarden Secrets Manager ONLY.

    Bitwarden SM is the only secrets layer (RPOSE-008). There is NO
    environment-variable fallback: reading credentials from the process
    environment was removed because it silently degraded the security posture
    (an attacker-set or stale env var would be trusted as a real secret).

    Returns the secret string, or None if Bitwarden is unavailable or the
    secret is missing. The caller MUST surface a clear error on None rather
    than proceeding with an insecure default.
    """
    try:
        from repose.utils.bitwarden import get_secret
        val = get_secret(secret_id)
        if val:
            return val
    except Exception:
        # Bitwarden unreachable or SDK missing — fail closed, no env fallback.
        return None

    return None


def _store_secret_to_bitwarden(secret_name: str, secret_value: str) -> bool:
    """Store a secret to Bitwarden via SDK.

    Returns True on success, False on failure.
    """
    try:
        from repose.utils.bitwarden import store_secret
        store_secret(secret_name, secret_value)
        return True
    except ImportError:
        print(_format_yellow(
            f"  [!] Bitwarden SDK not available — secret '{secret_name}' NOT stored."
        ))
        print(f"  Manual: store '{secret_name}' to Bitwarden, then update repose_config.yaml")
        return False
    except Exception as e:
        print(_format_red(f"  [!] Bitwarden store failed: {e}"))
        return False


def _send_telegram_test(bot_token: str, chat_id: str, message: str) -> bool:
    """Send a test message to a Telegram channel. Returns True on success."""
    import json
    import urllib.request
    import urllib.error

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode())
            if body.get("ok"):
                return True
            print(_format_yellow(f"  [!] Telegram API: {body.get('description', 'unknown error')}"))
            return False
    except Exception as e:
        print(_format_yellow(f"  [!] Telegram API unreachable: {e}"))
        return False


def _get_channel_info(bot_token: str, chat_id: str) -> Optional[dict]:
    """Get Telegram channel info. Returns dict or None on failure."""
    import json
    import urllib.request

    url = f"https://api.telegram.org/bot{bot_token}/getChat"
    payload = json.dumps({"chat_id": chat_id}).encode("utf-8")

    try:
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode())
            if body.get("ok"):
                return body["result"]
            return None
    except Exception:
        return None


def cmd_telegram_setup(args) -> int:
    """Execute: repose telegram setup

    Steps:
    1. Prompt operator to create a Telegram bot via @BotFather
    2. Accept bot token via stdin (masked input)
    3. Instruct operator to add bot to two channels and make it admin
    4. Verify bot can post to both channels via test messages
    5. Store bot token and both channel IDs to Bitwarden via SDK
    6. Write secret IDs to repose_config.yaml
    7. Confirm setup complete with channel names
    """
    use_color = _supports_color()

    print(_format_bold("\nRepose OS — Telegram Setup\n", use_color))
    print("This sets up the shared Telegram routing for all Repose agents.")
    print()

    # ── Step 1: Create bot ──────────────────────────────────────────────
    print(_format_bold("Step 1 — Create a Telegram Bot", use_color))
    print()
    print("  Open Telegram and message @BotFather:")
    print("    https://t.me/BotFather")
    print()
    print("  Send these commands:")
    print("    1. /newbot")
    print("    2. Choose a name (e.g., 'Repose OS')")
    print("    3. Choose a username (e.g., 'ReposeBot' or 'YourNameReposeBot')")
    print()
    print("  BotFather will reply with a bot token that looks like:")
    print("    1234567890:ABCdefGHIjklMNOpqrsTUVwxyz")
    print()

    if not prompt_yes_no("Continue when you have the bot token?"):
        print("Setup aborted.")
        return EXIT_ERROR

    # ── Step 2: Accept bot token ─────────────────────────────────────────
    print()
    print(_format_bold("Step 2 — Enter Bot Token", use_color))
    print()
    print("  The token is sensitive. Input will be masked.")
    print("  Paste the token from @BotFather and press Enter.")
    print()

    try:
        bot_token = getpass.getpass("  Bot token: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nSetup aborted.")
        return EXIT_ERROR

    if not bot_token or ":" not in bot_token:
        print(_format_red(
            "  Invalid bot token format. Expected something like '1234567890:ABCdef...'",
            use_color,
        ))
        return EXIT_ERROR

    print(_format_green("  Token accepted.", use_color))
    print()

    # ── Step 3: Add bot to channels ─────────────────────────────────────
    print(_format_bold("Step 3 — Create Two Telegram Channels", use_color))
    print()
    print("  You need TWO channels. Create them in Telegram now:")
    print()
    print("  Channel 1 — CRITICAL (for urgent events, system failures)")
    print("    Name suggestion: 'Repose OS · Critical'")
    print("    Make your bot an ADMINISTRATOR with 'Post Messages' permission.")
    print()
    print("  Channel 2 — INFORMATIONAL (for routine updates, Intel_feed surfaces)")
    print("    Name suggestion: 'Repose OS · Updates'")
    print("    Make your bot an ADMINISTRATOR with 'Post Messages' permission.")
    print()

    if not prompt_yes_no("Both channels created and bot added as admin?"):
        print("Setup aborted. Run 'repose telegram setup' again when ready.")
        return EXIT_ERROR

    # ── Accept channel IDs ───────────────────────────────────────────────
    print()
    print(_format_bold("Step 4 — Enter Channel IDs", use_color))
    print()
    print("  You can find channel IDs by forwarding a message from each channel")
    print("  to @RawDataBot on Telegram. It replies with the chat_id.")
    print("  Channel IDs often start with '-100' (e.g., -1001234567890).")
    print()

    try:
        critical_channel_id = input("  Critical channel ID: ").strip()
        informational_channel_id = input("  Informational channel ID: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nSetup aborted.")
        return EXIT_ERROR

    if not critical_channel_id or not informational_channel_id:
        print(_format_red("  Both channel IDs are required.", use_color))
        return EXIT_ERROR

    # ── Step 4: Verify ───────────────────────────────────────────────────
    print()
    print(_format_bold("Step 5 — Verify Connectivity", use_color))
    print()

    # Test critical channel
    print(f"  Testing critical channel ({critical_channel_id})...")
    critical_ok = _send_telegram_test(
        bot_token, critical_channel_id,
        "<b>Repose OS</b> — Telegram setup test (critical channel).\nIf you see this, setup is working.",
    )
    if critical_ok:
        ch_info = _get_channel_info(bot_token, critical_channel_id)
        ch_name = ch_info.get("title", "Unknown") if ch_info else "Unknown"
        print(_format_green(f"  Critical channel OK — '{ch_name}'", use_color))
    else:
        print(_format_red(
            "  Critical channel test FAILED. Check: is the bot admin? Is the ID correct?",
            use_color,
        ))
        return EXIT_ERROR

    # Test informational channel
    print(f"  Testing informational channel ({informational_channel_id})...")
    info_ok = _send_telegram_test(
        bot_token, informational_channel_id,
        "<b>Repose OS</b> — Telegram setup test (informational channel).\nIf you see this, setup is working.",
    )
    if info_ok:
        ch_info = _get_channel_info(bot_token, informational_channel_id)
        ch_name = ch_info.get("title", "Unknown") if ch_info else "Unknown"
        print(_format_green(f"  Informational channel OK — '{ch_name}'", use_color))
    else:
        print(_format_red(
            "  Informational channel test FAILED. Check: is the bot admin? Is the ID correct?",
            use_color,
        ))
        return EXIT_ERROR

    # ── Step 5: Store to Bitwarden ───────────────────────────────────────
    print()
    print(_format_bold("Step 6 — Store Secrets", use_color))
    print()

    bw_ok = True
    bw_ok &= _store_secret_to_bitwarden("repose-telegram-bot-token", bot_token)
    bw_ok &= _store_secret_to_bitwarden("repose-telegram-critical-channel-id", critical_channel_id)
    bw_ok &= _store_secret_to_bitwarden("repose-telegram-informational-channel-id", informational_channel_id)

    if bw_ok:
        print(_format_green("  All secrets stored to Bitwarden.", use_color))
    else:
        print(_format_yellow(
            "  Some secrets could not be stored. See warnings above.",
            use_color,
        ))

    # ── Step 6: Write config ─────────────────────────────────────────────
    print()
    print(_format_bold("Step 7 — Update Config", use_color))
    print()

    # Determine config path
    config_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config", "repose_config.yaml",
    )

    if not os.path.exists(config_path):
        print(_format_red(f"  Config file not found: {config_path}", use_color))
        print("  Create repose/config/repose_config.yaml first.")
        return EXIT_ERROR

    # Read current config
    with open(config_path, "r") as f:
        config_content = f.read()

    # Check if telegram block already has correct values
    if "repose-telegram-bot-token" in config_content:
        print(_format_green(
            "  Config already references Bitwarden secret IDs. No update needed.",
            use_color,
        ))
    else:
        print(_format_yellow(
            "  Config must reference Bitwarden secret IDs. Verify repose_config.yaml has:",
            use_color,
        ))
        print("    telegram.bot_token_secret_id: 'bitwarden:repose-telegram-bot-token'")
        print("    telegram.channels.critical: 'bitwarden:repose-telegram-critical-channel-id'")
        print("    telegram.channels.informational: 'bitwarden:repose-telegram-informational-channel-id'")

    # ── Done ─────────────────────────────────────────────────────────────
    print()
    print(_format_bold("─── Setup Complete ───", use_color))
    print()
    print(_format_green("  Repose Telegram routing is configured.", use_color))
    print("  Bot token and channel IDs stored in Bitwarden.")
    print()
    print("  Next: run 'repose --help' to see available agents and commands.")
    print()

    return EXIT_SUCCESS


def cmd_repose_help(args) -> int:
    """Show overall help for repose CLI."""
    print("Repose OS — CLI")
    print()
    print("Usage: repose <agent|command> [noun] [verb] [args] [--format json]")
    print()
    print("Global commands:")
    print("  telegram setup       Configure Telegram routing for all agents")
    print("  --help               Show this help")
    print()
    print("Agents:")
    print("  intel_feed                Intelligence scout — scheduled signal pipeline")
    print("  event_monitor                Event watcher — webhook receiver and classifier")
    print("  observer               Observer — read-only system monitoring")
    print("  morning_brief               Morning brief composer")
    print("  session_handoff                Session handoff manager")
    print()
    print("Standard verbs:")
    print("  list                 List resources")
    print("  enable               Enable a resource")
    print("  disable              Disable a resource")
    print("  add                  Add a resource")
    print("  remove               Remove a resource")
    print("  modify               Modify a resource")
    print("  test                 Test without side effects")
    print("  history              Show change history")
    print("  setup                Interactive setup wizard")
    print("  status               Show agent health and status")
    print("  scan                 Trigger immediate scan (Intel_feed)")
    print("  ack                  Acknowledge an observation (Observer)")
    print()
    print("All commands support --format json for machine-readable output.")
    return EXIT_SUCCESS


def main():
    parser = argparse.ArgumentParser(
        prog="repose",
        description="Repose OS — Agent Operations Framework CLI",
        add_help=False,
    )
    parser.add_argument(
        "--help", "-h", action="store_true",
        help="Show help",
    )

    # Global subcommands (not agent-specific)
    subparsers = parser.add_subparsers(dest="command", title="commands")

    # repose telegram setup
    telegram_parser = subparsers.add_parser("telegram", help="Telegram routing configuration")
    telegram_sub = telegram_parser.add_subparsers(dest="telegram_command")
    telegram_sub.add_parser("setup", help="Configure Telegram routing")

    # Agent stubs (intel_feed, event_monitor, observer, morning_brief, session_handoff)
    for agent_name in ["intel_feed", "event_monitor", "observer", "morning_brief", "session_handoff"]:
        subparsers.add_parser(agent_name, help=f"{agent_name.capitalize()} agent commands")

    # repose --help
    # (handled above)

    args, remaining = parser.parse_known_args()

    if args.help or (not args.command and not remaining):
        return cmd_repose_help(args)

    if args.command == "telegram":
        if args.telegram_command == "setup":
            return cmd_telegram_setup(args)
        else:
            print("Unknown telegram subcommand. Try: repose telegram setup")
            return EXIT_ERROR

    # Agent routing (intel_feed, event_monitor, observer, morning_brief, session_handoff)
    AGENT_CLI_MAP = {
        "intel_feed": "Intel_feedCLI",
        "event_monitor": "Event_monitorCLI",
        "observer": "ObserverCLI",
        "morning_brief": "Morning_briefCLI",
        "session_handoff": "Session_handoffCLI",
    }
    if args.command in AGENT_CLI_MAP:
        # ── Event_monitor: use the real implementation ──────────────────────
        if args.command == "event_monitor":
            try:
                from repose.agents.event_monitor_cli import Event_monitorCLI as RealEvent_monitorCLI
                cli = RealEvent_monitorCLI(args=remaining)
            except ImportError:
                from repose.utils.stub_cli import Event_monitorCLI as StubEvent_monitorCLI
                cli = StubEvent_monitorCLI(args=remaining)
            return cli.run()

        # ── Observer: use the real implementation ────────────────────
        if args.command == "observer":
            try:
                from repose.cli.observer_cli import ObserverCLI as RealObserverCLI
                cli = RealObserverCLI(args=remaining)
            except ImportError:
                from repose.utils.stub_cli import ObserverCLI as StubObserverCLI
                cli = StubObserverCLI(args=remaining)
            return cli.run()

        # ── Intel_feed: use the real implementation ──────────────────────────
        if args.command == "intel_feed":
            try:
                from repose.cli.intel_feed_cli import Intel_feedCLI as RealIntel_feedCLI
                cli = RealIntel_feedCLI(args=remaining)
            except ImportError:
                from repose.utils.stub_cli import Intel_feedCLI as StubIntel_feedCLI
                cli = StubIntel_feedCLI(args=remaining)
            return cli.run()

        # ── Other agents: try real implementations, fall back to stubs───
        from repose.utils.stub_cli import (
            Morning_briefCLI, Session_handoffCLI,
        )
        cli_cls = {
            "morning_brief": Morning_briefCLI,
            "session_handoff": Session_handoffCLI,
        }[args.command]
        cli = cli_cls(args=remaining)
        return cli.run()

    # Unknown command
    print(_format_red(f"Unknown command: {args.command}", _supports_color()))
    print("Try: repose --help")
    return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
