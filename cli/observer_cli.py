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
  repose observer admin test --subsystem execution_health
  repose observer admin test --subsystem substrate_health
  repose observer admin test --subsystem quality_drift

All commands support --format json. Deterministic ordering. Exit codes 0/1/2.
"""

import json
import logging
import re
import sys
from typing import Any, List, Optional

from repose.utils.cli_base import CLIBase, EXIT_SUCCESS, EXIT_ERROR, EXIT_WARNING

logger = logging.getLogger(__name__)


class ObserverCLI(CLIBase):
    """Observer observer CLI — quick-action and admin commands."""

    agent_name = "observer"
    nouns = [
        "observations",
        "ack",
        "status",
        "admin",
    ]

    def handle(self, noun: str, verb: str, args: List[str]) -> Any:
        """Dispatch to the appropriate handler based on noun + verb."""
        from repose.agents import observer

        if noun == "observations":
            return self._observations_handler(verb, args)
        elif noun == "ack":
            return self._ack_handler(verb, args)
        elif noun == "status":
            return observer.get_status()
        elif noun == "admin":
            if not args:
                return {"error": "admin requires a sub-noun", "valid": "agents, subsystems, thresholds, baseline, credentials, test"}
            sub_noun = args[0]
            sub_args = args[1:] if len(args) > 1 else []
            return self._admin_handler(sub_noun, verb, sub_args)
        else:
            return {"error": f"Unknown noun: {noun}"}

    # ── Quick-action: observations ────────────────────────────────────────

    def _observations_handler(self, verb: str, args: List[str]) -> Any:
        """Handle repose observer observations <verb>."""
        from repose.agents import observer

        if verb == "list":
            return self._observations_list(args)
        else:
            return {"error": f"Unknown observations verb: {verb}"}

    def _observations_list(self, args: List[str]) -> Any:
        """Handle repose observer observations list."""
        from repose.agents import observer

        # Parse optional flags from remaining args
        severity = None
        last_days = None

        i = 0
        while i < len(args):
            if args[i] == "--severity" and i + 1 < len(args):
                severity = args[i + 1]
                i += 2
            elif args[i] == "--last" and i + 1 < len(args):
                match = re.match(r"(\d+)d", args[i + 1])
                if match:
                    last_days = int(match.group(1))
                i += 2
            else:
                i += 1

        obs = observer.get_observations(
            severity=severity,
            last_days=last_days,
        )
        return obs

    # ── Quick-action: ack ──────────────────────────────────────────────────

    def _ack_handler(self, verb: str, args: List[str]) -> Any:
        """Handle repose observer ack <observation_id> --type <type>."""
        from repose.agents import observer

        if verb not in ("noted", "wont_fix", "resolved"):
            return {"error": f"Unknown ack verb: {verb}. Use --type noted|wont_fix|resolved"}

        # For the CLI, the verb IS the ack_type (from subcommand routing)
        # But standard usage is: repose observer ack <id> --type <type>
        # Handle both patterns

        obs_id = None
        ack_type = verb
        expiry_days = None

        i = 0
        while i < len(args):
            if args[i] == "--type" and i + 1 < len(args):
                ack_type = args[i + 1]
                i += 2
            elif args[i] == "--expiry-days" and i + 1 < len(args):
                expiry_days = int(args[i + 1])
                i += 2
            elif not obs_id and not args[i].startswith("--"):
                obs_id = args[i]
                i += 1
            else:
                i += 1

        if not obs_id:
            return {"error": "observation_id required"}

        try:
            result = observer.ack_observation(obs_id, ack_type, expiry_days)
            return result
        except ValueError as e:
            return {"error": str(e)}

    # ── Admin commands ─────────────────────────────────────────────────────

    def _admin_handler(self, sub_noun: str, verb: str, args: List[str]) -> Any:
        """Handle repose observer admin <sub_noun> <verb> [args]."""
        from repose.agents import observer

        if sub_noun == "agents":
            return self._admin_agents(verb, args)
        elif sub_noun == "subsystems":
            return self._admin_subsystems(verb, args)
        elif sub_noun == "thresholds":
            return self._admin_thresholds(verb, args)
        elif sub_noun == "baseline":
            return self._admin_baseline(verb, args)
        elif sub_noun == "credentials":
            return self._admin_credentials(verb, args)
        elif sub_noun == "test":
            return self._admin_test(verb, args)
        else:
            return {"error": f"Unknown admin sub-noun: {sub_noun}"}

    def _admin_agents(self, verb: str, args: List[str]) -> Any:
        """Handle repose observer admin agents <verb>."""
        from repose.agents import observer

        if verb == "list":
            cfg = observer.get_observer_config()
            agents = cfg.get("execution_health", {}).get("observed_agents", {})
            return [
                {
                    "agent": name,
                    "enabled": c.get("enabled", False),
                    "namespace": c.get("namespace", ""),
                    "expected_writes_per_day": c.get("expected_writes_per_day"),
                    "max_silence_hours": c.get("max_silence_hours"),
                    "max_error_rate_per_hour": c.get("max_error_rate_per_hour"),
                }
                for name, c in sorted(agents.items())
            ]

        elif verb == "enable":
            if not args:
                return {"error": "agent name required"}
            return observer.enable_agent(args[0])

        elif verb == "disable":
            if not args:
                return {"error": "agent name required"}
            return observer.disable_agent(args[0])

        elif verb == "set":
            if len(args) < 3:
                return {"error": "usage: admin agents set <agent> expected_writes_per_day <N|null>"}
            agent_name = args[0]
            key = args[1]
            value = args[2]

            cfg = observer.get_observer_config()
            agents = cfg.get("execution_health", {}).get("observed_agents", {})
            if agent_name not in agents:
                return {"error": f"Agent '{agent_name}' not found"}

            if value.lower() == "null":
                parsed = None
            else:
                try:
                    parsed = int(value)
                except ValueError:
                    return {"error": f"Invalid value: {value}"}

            agents[agent_name][key] = parsed
            updates = {"execution_health": {"observed_agents": agents}}
            cfg = observer.update_observer_config(updates)
            return {"agent": agent_name, key: parsed, "status": "updated"}

        else:
            return {"error": f"Unknown agents verb: {verb}"}

    def _admin_subsystems(self, verb: str, args: List[str]) -> Any:
        """Handle repose observer admin subsystems <verb>."""
        from repose.agents import observer

        if verb == "list":
            cfg = observer.get_observer_config()
            return [
                {
                    "subsystem": name,
                    "enabled": cfg.get(name, {}).get("enabled", False),
                    "cron": cfg.get(name, {}).get("cron", ""),
                }
                for name in ["execution_health", "substrate_health", "quality_drift"]
            ]

        elif verb in ("enable", "disable"):
            if not args:
                return {"error": "subsystem name required"}
            sub_name = args[0]
            if sub_name not in ("execution_health", "substrate_health", "quality_drift"):
                return {"error": f"Unknown subsystem: {sub_name}"}

            updates = {sub_name: {"enabled": verb == "enable"}}
            cfg = observer.update_observer_config(updates)
            return {"subsystem": sub_name, "enabled": verb == "enable", "status": "updated"}

        else:
            return {"error": f"Unknown subsystems verb: {verb}"}

    def _admin_thresholds(self, verb: str, args: List[str]) -> Any:
        """Handle repose observer admin thresholds set <path> <value>."""
        from repose.agents import observer

        if verb == "set":
            if len(args) < 2:
                return {"error": "usage: admin thresholds set <path> <value>"}

            path = args[0]
            value_str = args[1]

            try:
                value = float(value_str)
            except ValueError:
                return {"error": f"Invalid float value: {value_str}"}

            # Parse dotted path
            parts = path.split(".")
            updates = {}
            current = updates
            for i, part in enumerate(parts[:-1]):
                current[part] = {}
                current = current[part]
            current[parts[-1]] = value

            cfg = observer.update_observer_config(updates)
            return {"path": path, "value": value, "status": "updated"}

        else:
            return {"error": f"Unknown thresholds verb: {verb}"}

    def _admin_baseline(self, verb: str, args: List[str]) -> Any:
        """Handle repose observer admin baseline recompute."""
        from repose.agents import observer

        if verb == "recompute":
            target_agent = None
            i = 0
            while i < len(args):
                if args[i] == "--agent" and i + 1 < len(args):
                    target_agent = args[i + 1]
                    i += 2
                else:
                    i += 1

            # Trigger quality drift check to recompute baselines
            obs = observer.check_quality_drift(agent=target_agent)
            return {
                "status": "recomputed",
                "agent": target_agent or "all",
                "observations_generated": len(obs),
            }

        else:
            return {"error": f"Unknown baseline verb: {verb}"}

    def _admin_credentials(self, verb: str, args: List[str]) -> Any:
        """Handle repose observer admin credentials setup."""
        from repose.agents import observer

        if verb == "setup":
            result = observer.setup_credentials()
            return result
        else:
            return {"error": f"Unknown credentials verb: {verb}"}

    def _admin_test(self, verb: str, args: List[str]) -> Any:
        """Handle repose observer admin test --subsystem <name>."""
        from repose.agents import observer

        subsystem = None
        i = 0
        while i < len(args):
            if args[i] == "--subsystem" and i + 1 < len(args):
                subsystem = args[i + 1]
                i += 2
            else:
                i += 1

        if not subsystem:
            return {"error": "--subsystem required"}

        if subsystem == "execution_health":
            obs = observer.check_execution_health()
        elif subsystem == "substrate_health":
            obs = observer.check_substrate_health()
        elif subsystem == "quality_drift":
            obs = observer.check_quality_drift()
        else:
            return {"error": f"Unknown subsystem: {subsystem}"}

        return {
            "subsystem": subsystem,
            "status": "test_complete",
            "observations_found": len(obs),
            "observations": obs,
        }


def main():
    """Entry point for Observer CLI."""
    cli = ObserverCLI()
    sys.exit(cli.run())


if __name__ == "__main__":
    main()
