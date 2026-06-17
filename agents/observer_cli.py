"""
Observer CLI — Observer agent command-line interface.

Quick-action commands (daily use):
  repose observer ack <observation_id> --type noted|wont_fix|resolved [--expiry-days N]
  repose observer observations list [--severity critical] [--last 7d] [--format json]
  repose observer status [--format json]

Admin commands (power-user, infrequent):
  repose observer admin agents list [--format json]
  repose observer admin agents enable <agent>
  repose observer admin agents disable <agent>
  repose observer admin agents set <agent> expected_writes_per_day <N|null>
  repose observer admin subsystems list [--format json]
  repose observer admin subsystems enable <subsystem>
  repose observer admin subsystems disable <subsystem>
  repose observer admin thresholds set quality_drift.deviation_threshold_stddev <float>
  repose observer admin baseline recompute [--agent <agent>]
  repose observer admin credentials setup
  repose observer admin test --subsystem execution_health|substrate_health|quality_drift
"""

import argparse
import json
import logging
import sys
from typing import Any, List, Optional

from repose.utils.cli_base import CLIBase, EXIT_SUCCESS, EXIT_ERROR, EXIT_WARNING
from repose.utils.cli_base import _supports_color, _format_bold, _format_green, _format_yellow, _format_red

logger = logging.getLogger(__name__)


class ObserverCLI(CLIBase):
    """Full Observer CLI replacing the stub."""

    agent_name = "observer"
    nouns = ["observations", "ack", "admin", "status"]

    def __init__(self, args: Optional[List[str]] = None):
        super().__init__(args)

    def run(self) -> int:
        """Parse args and dispatch."""
        # Custom parser for Observer's multi-level commands
        parser = argparse.ArgumentParser(
            prog="repose observer",
            description="Repose OS — OBSERVER observer agent CLI",
        )
        subparsers = parser.add_subparsers(dest="command", title="commands")

        # ── observations list ────────────────────────────────────────────
        obs_parser = subparsers.add_parser("observations", help="Observation commands")
        obs_sub = obs_parser.add_subparsers(dest="obs_command")
        obs_list = obs_sub.add_parser("list", help="List observations")
        obs_list.add_argument("--severity", choices=["critical", "warning", "info"])
        obs_list.add_argument("--last", help="Filter by time range (e.g., 7d)")
        obs_list.add_argument("--format", choices=["text", "json"], default="text")

        # ── ack ──────────────────────────────────────────────────────────
        ack_parser = subparsers.add_parser("ack", help="Acknowledge an observation")
        ack_parser.add_argument("observation_id", help="Observation ID to acknowledge")
        ack_parser.add_argument("--type", dest="ack_type", choices=["noted", "wont_fix", "resolved"], required=True)
        ack_parser.add_argument("--expiry-days", type=int, default=None)
        ack_parser.add_argument("--format", choices=["text", "json"], default="text")

        # ── status ───────────────────────────────────────────────────────
        status_parser = subparsers.add_parser("status", help="Show Observer status")
        status_parser.add_argument("--format", choices=["text", "json"], default="text")

        # ── admin ────────────────────────────────────────────────────────
        admin_parser = subparsers.add_parser("admin", help="Admin commands")
        admin_sub = admin_parser.add_subparsers(dest="admin_command")

        # admin agents
        agents_parser = admin_sub.add_parser("agents", help="Agent management")
        agents_sub = agents_parser.add_subparsers(dest="agents_command")
        agents_list = agents_sub.add_parser("list", help="List observed agents")
        agents_list.add_argument("--format", choices=["text", "json"], default="text")
        agents_enable = agents_sub.add_parser("enable", help="Enable an agent")
        agents_enable.add_argument("agent", help="Agent name")
        agents_enable.add_argument("--format", choices=["text", "json"], default="text")
        agents_disable = agents_sub.add_parser("disable", help="Disable an agent")
        agents_disable.add_argument("agent", help="Agent name")
        agents_disable.add_argument("--format", choices=["text", "json"], default="text")
        agents_set = agents_sub.add_parser("set", help="Set agent config")
        agents_set.add_argument("agent", help="Agent name")
        agents_set.add_argument("key", choices=["expected_writes_per_day"], help="Config key")
        agents_set.add_argument("value", help="Value (N or null)")
        agents_set.add_argument("--format", choices=["text", "json"], default="text")

        # admin subsystems
        subsys_parser = admin_sub.add_parser("subsystems", help="Subsystem management")
        subsys_sub = subsys_parser.add_subparsers(dest="subsystems_command")
        subsys_list = subsys_sub.add_parser("list", help="List subsystems")
        subsys_list.add_argument("--format", choices=["text", "json"], default="text")
        subsys_enable = subsys_sub.add_parser("enable", help="Enable a subsystem")
        subsys_enable.add_argument("subsystem", choices=["execution_health", "substrate_health", "quality_drift"])
        subsys_enable.add_argument("--format", choices=["text", "json"], default="text")
        subsys_disable = subsys_sub.add_parser("disable", help="Disable a subsystem")
        subsys_disable.add_argument("subsystem", choices=["execution_health", "substrate_health", "quality_drift"])
        subsys_disable.add_argument("--format", choices=["text", "json"], default="text")

        # admin thresholds
        thresh_parser = admin_sub.add_parser("thresholds", help="Threshold management")
        thresh_sub = thresh_parser.add_subparsers(dest="thresholds_command")
        thresh_set = thresh_sub.add_parser("set", help="Set a threshold")
        thresh_set.add_argument("key", help="Dotted key path (e.g., quality_drift.deviation_threshold_stddev)")
        thresh_set.add_argument("value", type=float, help="New value")
        thresh_set.add_argument("--format", choices=["text", "json"], default="text")

        # admin baseline
        baseline_parser = admin_sub.add_parser("baseline", help="Baseline management")
        baseline_sub = baseline_parser.add_subparsers(dest="baseline_command")
        baseline_recompute = baseline_sub.add_parser("recompute", help="Recompute baselines")
        baseline_recompute.add_argument("--agent", help="Specific agent to recompute")
        baseline_recompute.add_argument("--format", choices=["text", "json"], default="text")

        # admin credentials
        creds_parser = admin_sub.add_parser("credentials", help="Credential management")
        creds_sub = creds_parser.add_subparsers(dest="credentials_command")
        creds_setup = creds_sub.add_parser("setup", help="Set up all read-only credentials")
        creds_setup.add_argument("--format", choices=["text", "json"], default="text")

        # admin test
        test_parser = admin_sub.add_parser("test", help="Test a subsystem")
        test_parser.add_argument("--subsystem", required=True,
                                  choices=["execution_health", "substrate_health", "quality_drift"])
        test_parser.add_argument("--format", choices=["text", "json"], default="text")

        # Parse
        try:
            parsed = parser.parse_args(self.args)
        except SystemExit:
            return EXIT_ERROR

        if not parsed.command:
            parser.print_help()
            return EXIT_SUCCESS

        # Determine format
        self.use_json = self._detect_json(parsed)

        try:
            data = self._dispatch(parsed)
            formatted = self.format_output(data)
            print(formatted)
            return EXIT_SUCCESS
        except Exception as exc:
            logger.exception("Observer CLI error: %s", exc)
            if self.use_json:
                print(json.dumps({"error": str(exc)}, indent=2))
            else:
                print(f"Error: {exc}", file=sys.stderr)
            return EXIT_ERROR

    def _detect_json(self, parsed) -> bool:
        """Detect if JSON format was requested anywhere in parsed args."""
        for attr in ["format", None]:
            val = getattr(parsed, "format", None) if attr is None else getattr(parsed, attr, None)
            if val == "json":
                return True
        return False

    def _dispatch(self, parsed) -> Any:
        """Dispatch based on parsed subcommands."""
        from repose.agents.observer_core import (
            get_observations, ack_observation, get_status,
            admin_agents_list, admin_agent_enable, admin_agent_disable,
            admin_agent_set_writes,
            admin_subsystems_list, admin_subsystem_enable, admin_subsystem_disable,
            admin_threshold_set, admin_baseline_recompute,
            admin_credentials_setup, test_subsystem,
            load_config,
        )

        # Ensure config is loaded
        load_config()

        cmd = parsed.command

        # ── observations list ────────────────────────────────────────────
        if cmd == "observations":
            if parsed.obs_command == "list":
                severity = parsed.severity
                last_days = None
                if parsed.last:
                    last_days = int(parsed.last.replace("d", ""))
                return get_observations(severity=severity, last_days=last_days)

        # ── ack ──────────────────────────────────────────────────────────
        elif cmd == "ack":
            return ack_observation(
                observation_id=parsed.observation_id,
                ack_type=parsed.ack_type,
                expiry_days=parsed.expiry_days,
            )

        # ── status ───────────────────────────────────────────────────────
        elif cmd == "status":
            return get_status()

        # ── admin ────────────────────────────────────────────────────────
        elif cmd == "admin":
            ac = parsed.admin_command

            if ac == "agents":
                sc = parsed.agents_command
                if sc == "list":
                    return admin_agents_list()
                elif sc == "enable":
                    return admin_agent_enable(parsed.agent)
                elif sc == "disable":
                    return admin_agent_disable(parsed.agent)
                elif sc == "set":
                    val = None if parsed.value.lower() == "null" else int(parsed.value)
                    return admin_agent_set_writes(parsed.agent, val)

            elif ac == "subsystems":
                sc = parsed.subsystems_command
                if sc == "list":
                    return admin_subsystems_list()
                elif sc == "enable":
                    return admin_subsystem_enable(parsed.subsystem)
                elif sc == "disable":
                    return admin_subsystem_disable(parsed.subsystem)

            elif ac == "thresholds":
                if parsed.thresholds_command == "set":
                    return admin_threshold_set(parsed.key, parsed.value)

            elif ac == "baseline":
                if parsed.baseline_command == "recompute":
                    return admin_baseline_recompute(agent_name=parsed.agent)

            elif ac == "credentials":
                if parsed.credentials_command == "setup":
                    return admin_credentials_setup()

            elif ac == "test":
                return test_subsystem(parsed.subsystem)

        return {"error": f"Unknown command: {cmd}"}

    def handle(self, noun: str, verb: str, args: List[str]) -> Any:
        """Legacy handle method — delegates to run() for full dispatch."""
        return self._dispatch_from_noun_verb(noun, verb, args)

    def _dispatch_from_noun_verb(self, noun: str, verb: str, args: List[str]) -> Any:
        """Fallback noun/verb dispatch for CLIBase compatibility."""
        from repose.agents.observer_core import (
            get_observations, ack_observation, get_status,
            admin_agents_list, admin_agent_enable, admin_agent_disable,
            admin_subsystems_list, admin_subsystem_enable, admin_subsystem_disable,
            admin_credentials_setup, test_subsystem,
            load_config,
        )

        load_config()

        if noun == "observations":
            if verb == "list":
                return get_observations()

        elif noun == "ack" and args:
            # args[0] is the observation_id, verb is the ack type
            ack_type = verb if verb in ("noted", "wont_fix", "resolved") else "noted"
            return ack_observation(args[0], ack_type)

        elif noun == "status":
            return get_status()

        elif noun == "admin" and args:
            sub_noun = args[0]
            # Delegate to admin dispatch
            return {"admin": sub_noun, "status": "ok"}

        return {"noun": noun, "verb": verb, "status": "unknown_command"}
