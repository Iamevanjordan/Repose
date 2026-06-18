"""
Base CLI utility for all Repose OS agent CLIs.

Standard verb pattern (ALL agents must follow):
    repose <agent> <noun> <verb> [args] [--format json]

Where:
    <noun> = sources, agents, subsystems, observations, ack, credentials, channels
    <verb> = list | enable | disable | add | remove | modify | test | history | setup

Features:
    --format json flag on every data-returning command
    Confirmation prompt for destructive operations
    Restart prompt for config-changing operations (enable/disable)
    Exit codes: 0 = success, 1 = error, 2 = warning / user cancelled
"""

import argparse
import json
import logging
import subprocess
import sys
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

VALID_NOUNS = {"sources", "agents", "subsystems", "observations", "ack", "credentials", "channels", "ingress"}
VALID_VERBS = {"list", "enable", "disable", "add", "remove", "modify", "test", "history", "setup", "restart"}

# Verbs that trigger confirmation prompts
DESTRUCTIVE_VERBS = {"disable", "remove"}

# Verbs that never prompt
READ_ONLY_VERBS = {"list", "history", "test", "status"}

# Exit codes
EXIT_SUCCESS = 0
EXIT_ERROR = 1
EXIT_WARNING = 2


def _supports_color() -> bool:
    """Check if the terminal supports ANSI color codes."""
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def _format_bold(text: str, use_color: bool = True) -> str:
    if not use_color:
        return text
    return f"\033[1m{text}\033[0m"


def _format_green(text: str, use_color: bool = True) -> str:
    if not use_color:
        return text
    return f"\033[32m{text}\033[0m"


def _format_yellow(text: str, use_color: bool = True) -> str:
    if not use_color:
        return text
    return f"\033[33m{text}\033[0m"


def _format_red(text: str, use_color: bool = True) -> str:
    if not use_color:
        return text
    return f"\033[31m{text}\033[0m"


def _format_dim(text: str, use_color: bool = True) -> str:
    if not use_color:
        return text
    return f"\033[2m{text}\033[0m"


def prompt_yes_no(question: str) -> bool:
    """Ask a yes/no question. Returns True for yes, False for no."""
    try:
        response = input(f"{question} [y/N]: ").strip().lower()
        return response in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


class CLIBase:
    """Base CLI class that all agent CLIs inherit from.

    Subclasses set `agent_name` and `nouns` and override `handle()`.
    """

    agent_name: str = ""
    nouns: List[str] = []
    DESTRUCTIVE_VERBS = DESTRUCTIVE_VERBS
    READ_ONLY_VERBS = READ_ONLY_VERBS

    def __init__(self, args: Optional[List[str]] = None):
        if not self.agent_name:
            raise ValueError("Subclass must set agent_name")
        self.args = args or sys.argv[1:]
        self.use_json = False
        self._parsed_args = None

    def handle(self, noun: str, verb: str, args: List[str]) -> Any:
        """Handle a CLI command. Subclasses must override.

        Args:
            noun: The noun group (e.g., 'sources')
            verb: The verb (e.g., 'list')
            args: Additional positional arguments

        Returns:
            Data to output (list, dict, or scalar), or raises an exception
        """
        raise NotImplementedError(
            f"Subclass must implement handle() for {self.agent_name} {noun} {verb}"
        )

    def format_output(self, data: Any) -> str:
        """Format handler output for display.

        Args:
            data: Data from handle().

        Returns:
            Formatted string for stdout.
        """
        if self.use_json:
            return self._format_json(data)
        return self._format_text(data)

    def _format_json(self, data: Any) -> str:
        """Format data as JSON with deterministic ordering."""
        if isinstance(data, list):
            # Sort by timestamp or id if available
            try:
                data = sorted(data, key=lambda x: (
                    x.get("timestamp", x.get("id", "")) if isinstance(x, dict) else str(x)
                ))
            except (TypeError, AttributeError):
                pass
        return json.dumps(data, indent=2, sort_keys=True, default=str)

    def _format_text(self, data: Any) -> str:
        """Format data as human-readable text."""
        if isinstance(data, list):
            if not data:
                return "(no results)"
            lines = []
            for item in data:
                if isinstance(item, dict):
                    for key, value in item.items():
                        lines.append(f"  {key}: {value}")
                    lines.append("")
                else:
                    lines.append(str(item))
            return "\n".join(lines)
        elif isinstance(data, dict):
            lines = []
            for key, value in data.items():
                lines.append(f"  {key}: {value}")
            return "\n".join(lines)
        else:
            return str(data)

    def confirm_destructive(self, noun: str, verb: str, item_id: str) -> bool:
        """Ask for confirmation before a destructive operation.

        Returns True if the user confirms, False otherwise.
        """
        if verb not in DESTRUCTIVE_VERBS:
            return True

        try:
            response = input(
                f"\n{verb.capitalize()}ing {noun} '{item_id}' for {self.agent_name}. "
                f"This cannot be easily undone. [y/N]: "
            ).strip().lower()
            return response in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return False

    def prompt_restart(self, noun: str, verb: str, item_id: str) -> bool:
        """Ask whether to restart the agent worker after a config change.

        Returns True if user wants to restart.
        """
        if verb not in DESTRUCTIVE_VERBS:  # enable/disable prompts restart
            # Actually, enable prompts restart too
            if verb not in ("enable", "disable"):
                return False

        try:
            response = input(
                f"\n{verb.capitalize()}d. Config updated. "
                f"Restart {self.agent_name} worker to activate? [y/N]: "
            ).strip().lower()
            if response in ("y", "yes"):
                try:
                    subprocess.run(
                        ["systemctl", "restart", f"repose-{self.agent_name}-worker.service"],
                        capture_output=True, timeout=10,
                    )
                except Exception:
                    pass
                return True
            else:
                print(f"\nRun 'repose {self.agent_name} restart' when ready.")
                return False
        except (EOFError, KeyboardInterrupt):
            return False

    def run(self) -> int:
        """Parse args and dispatch to the appropriate handler.

        Returns exit code (0, 1, or 2).
        """
        # Parse command line
        parser = argparse.ArgumentParser(
            prog=f"repose {self.agent_name}",
            description=f"Repose OS — {self.agent_name.upper()} agent CLI",
        )
        parser.add_argument("noun", nargs="?", choices=self.nouns, help="Thing to act on")
        parser.add_argument("verb", nargs="?", choices=sorted(VALID_VERBS), help="Action to perform")
        parser.add_argument("id", nargs="?", default=None, help="Target ID")
        parser.add_argument("--format", choices=["text", "json"], default=None, help="Output format")
        parser.add_argument("--yes", action="store_true", help="Skip confirmation prompts")

        parsed = parser.parse_args(self.args)
        self._parsed_args = parsed

        # Set format mode
        if parsed.format == "json":
            self.use_json = True

        # Validate noun/verb
        if not parsed.noun or not parsed.verb:
            parser.print_help()
            return 0

        noun = parsed.noun
        verb = parsed.verb

        # ── Destructive confirmation ──────────────────────────────────
        if verb in DESTRUCTIVE_VERBS and not getattr(parsed, "yes", False):
            item_id = parsed.id or "all"
            if not self.confirm_destructive(noun, verb, item_id):
                return 2  # warning: user cancelled

        # ── Execute handler ───────────────────────────────────────────
        try:
            extra_args = [parsed.id] if parsed.id else []
            data = self.handle(noun, verb, extra_args)
            formatted = self.format_output(data)
            print(formatted)

            # ── Restart prompt for config changes ─────────────────────
            if verb in ("enable", "disable") and not self.use_json:
                self.prompt_restart(noun, verb, parsed.id or "")

            return 0
        except Exception as exc:
            logger.exception(f"CLI error: {exc}")
            print(f"Error: {exc}", file=sys.stderr)
            return 1
