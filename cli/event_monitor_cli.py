"""
Event_monitor CLI — repose event_monitor <noun> <verb> [args] [--format json]

Nouns: sources, events, ingress, stripe, github, form, status
Verbs: list, enable, disable, setup, test, history, set-repos, add-endpoint, restart, status
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, List

from repose.utils.cli_base import CLIBase, EXIT_SUCCESS, EXIT_ERROR, EXIT_WARNING
from repose.utils.cli_base import prompt_yes_no, _supports_color, _format_bold, _format_green, _format_yellow, _format_red


class Event_monitorCLI(CLIBase):
    """Event_monitor CLI — webhook receiver and event classification pipeline."""

    agent_name = "event_monitor"
    nouns = ["sources", "events", "ingress", "stripe", "github", "form", "status"]

    def handle(self, noun: str, verb: str, args: List[str]) -> Any:
        # ── sources ──────────────────────────────────────────────────
        if noun == "sources":
            return self._handle_sources(verb, args)

        # ── events ───────────────────────────────────────────────────
        if noun == "events":
            return self._handle_events(verb, args)

        # ── ingress ──────────────────────────────────────────────────
        if noun == "ingress":
            return self._handle_ingress(verb, args)

        # ── stripe ───────────────────────────────────────────────────
        if noun == "stripe":
            return self._handle_stripe(verb, args)

        # ── github ───────────────────────────────────────────────────
        if noun == "github":
            return self._handle_github(verb, args)

        # ── form ──────────────────────────────────────────────────
        if noun == "form":
            return self._handle_form(verb, args)

        # ── status ───────────────────────────────────────────────────
        if noun == "status":
            return self._handle_status(verb, args)

        raise ValueError(f"Unknown noun: {noun}")

    # ── Sources ──────────────────────────────────────────────────────

    def _handle_sources(self, verb: str, args: List[str]) -> Any:
        from repose.agents.event_monitor import get_config

        cfg = get_config()
        sources = cfg.get("sources", {})

        if verb == "list":
            result = []
            for name, scfg in sources.items():
                result.append({
                    "source": name,
                    "enabled": scfg.get("enabled", False),
                    "endpoint": scfg.get("endpoint", ""),
                    "dedup_strategy": scfg.get("dedup_strategy", ""),
                    "dedup_ttl_seconds": scfg.get("dedup_ttl_seconds", 0),
                })
            return result

        if verb == "enable":
            source = args[0] if args else ""
            if source not in sources:
                return {"error": f"Unknown source: {source}"}
            sources[source]["enabled"] = True
            from repose.agents.event_monitor import _save_config
            _save_config(cfg)
            return {"source": source, "enabled": True}

        if verb == "disable":
            source = args[0] if args else ""
            if source not in sources:
                return {"error": f"Unknown source: {source}"}
            sources[source]["enabled"] = False
            from repose.agents.event_monitor import _save_config
            _save_config(cfg)
            return {"source": source, "enabled": False}

        if verb == "history":
            return [{"source": s, "history": "No change history available (MVP)"} for s in sources]

        raise ValueError(f"Unknown verb for sources: {verb}")

    # ── Events ────────────────────────────────────────────────────────

    def _handle_events(self, verb: str, args: List[str]) -> Any:
        if verb != "list":
            raise ValueError(f"Unknown verb for events: {verb}")

        from repose.agents.event_monitor import get_events

        # Parse --lane and --last from args
        lane = None
        last_hours = 24
        i = 0
        while i < len(args):
            if args[i] == "--lane" and i + 1 < len(args):
                lane = args[i + 1]
                i += 2
            elif args[i] == "--last":
                if i + 1 < len(args):
                    try:
                        val = args[i + 1]
                        last_hours = int(val.replace("h", ""))
                    except ValueError:
                        pass
                    i += 2
                else:
                    i += 1
            else:
                i += 1

        events = get_events(namespace=None, lane=lane, limit=200)
        return events

    # ── Ingress ──────────────────────────────────────────────────────

    def _handle_ingress(self, verb: str, args: List[str]) -> Any:
        if verb == "setup":
            from repose.agents.event_monitor import get_config
            cfg = get_config()
            ingress = cfg.get("webhook_ingress", {})
            return {
                "agent": "event_monitor",
                "ingress_type": ingress.get("type", "cloudflared"),
                "tunnel_name": ingress.get("tunnel_name", ""),
                "listen_port": ingress.get("listen_port", 8080),
                "setup_note": (
                    "Cloudflare Tunnel setup requires: "
                    "1) Install cloudflared, "
                    "2) cloudflared tunnel login, "
                    "3) cloudflared tunnel create repose-event_monitor, "
                    "4) cloudflared tunnel route dns repose-event_monitor your-webhook-domain.example.com, "
                    "5) cloudflared tunnel run repose-event_monitor"
                ),
            }

        if verb == "status":
            from repose.agents.event_monitor import get_config, get_server
            cfg = get_config()
            ingress = cfg.get("webhook_ingress", {})
            server = get_server()
            return {
                "ingress_type": ingress.get("type", "cloudflared"),
                "tunnel_name": ingress.get("tunnel_name", ""),
                "listen_port": ingress.get("listen_port", 8080),
                "server_running": server is not None,
                "public_url": ingress.get("public_url_secret_id", "bitwarden:repose-event_monitor-public-url"),
            }

        raise ValueError(f"Unknown verb for ingress: {verb}")

    # ── Stripe ────────────────────────────────────────────────────────

    def _handle_stripe(self, verb: str, args: List[str]) -> Any:
        if verb == "setup":
            use_color = _supports_color()
            print(_format_bold("\nEvent_monitor — Stripe Webhook Setup\n", use_color))
            print("This configures Event_monitor to receive Stripe webhooks.")
            print()
            print("Prerequisites:")
            print("  1. Stripe account with admin access")
            print("  2. Event_monitor webhook endpoint: https://your-webhook-domain.example.com/webhooks/stripe")
            print()
            if not prompt_yes_no("Continue?"):
                return {"status": "cancelled"}

            import getpass
            try:
                secret = getpass.getpass("  Stripe signing secret (whsec_...): ").strip()
            except (EOFError, KeyboardInterrupt):
                return {"status": "cancelled"}

            if not secret.startswith("whsec_"):
                print(_format_red("  Invalid secret format. Should start with 'whsec_'.", use_color))
                return {"status": "error", "reason": "invalid_secret_format"}

            from repose.agents.event_monitor import setup_stripe
            result = setup_stripe(secret)
            print(_format_green(f"\n  Stripe configured. Signature verification enabled.", use_color))
            print(f"  Next: configure Stripe dashboard to send webhooks to https://your-webhook-domain.example.com/webhooks/stripe")
            return result

        if verb == "test":
            return self._handle_test("stripe", args)

        raise ValueError(f"Unknown verb for stripe: {verb}")

    # ── GitHub ────────────────────────────────────────────────────────

    def _handle_github(self, verb: str, args: List[str]) -> Any:
        if verb == "setup":
            use_color = _supports_color()
            print(_format_bold("\nEvent_monitor — GitHub Webhook Setup\n", use_color))
            print("This configures Event_monitor to receive GitHub webhooks.")
            print()
            print("Prerequisites:")
            print("  1. GitHub repo admin access")
            print("  2. Event_monitor webhook endpoint: https://your-webhook-domain.example.com/webhooks/github")
            print()

            if not prompt_yes_no("Continue?"):
                return {"status": "cancelled"}

            import getpass
            try:
                secret = getpass.getpass("  GitHub webhook secret: ").strip()
            except (EOFError, KeyboardInterrupt):
                return {"status": "cancelled"}

            if not secret:
                print(_format_red("  Secret is required.", use_color))
                return {"status": "error", "reason": "missing_secret"}

            from repose.agents.event_monitor import setup_github
            result = setup_github(secret)
            print(_format_green(f"\n  GitHub configured. Signature verification enabled.", use_color))
            return result

        if verb == "set-repos":
            repo_pattern = args[0] if args else "your-org/*"
            from repose.agents.event_monitor import get_config
            cfg = get_config()
            cfg["sources"]["github"]["monitored_repos"] = [repo_pattern]
            from repose.agents.event_monitor import _save_config
            _save_config(cfg)
            return {"source": "github", "monitored_repos": [repo_pattern]}

        if verb == "test":
            return self._handle_test("github", args)

        raise ValueError(f"Unknown verb for github: {verb}")

    # ── Form ──────────────────────────────────────────────────────────

    def _handle_form(self, verb: str, args: List[str]) -> Any:
        if verb == "add-endpoint":
            name = ""
            secret_id = ""
            i = 0
            while i < len(args):
                if args[i] == "--name" and i + 1 < len(args):
                    name = args[i + 1]
                    i += 2
                elif args[i] == "--secret-id" and i + 1 < len(args):
                    secret_id = args[i + 1]
                    i += 2
                else:
                    i += 1

            if not name:
                return {"error": "--name is required"}
            if not secret_id:
                return {"error": "--secret-id is required"}

            from repose.agents.event_monitor import get_config
            cfg = get_config()
            form_cfg = cfg.get("sources", {}).get("form", {})
            endpoints = form_cfg.get("endpoints", [])
            endpoints.append({"name": name, "secret_id": secret_id})
            form_cfg["endpoints"] = endpoints
            from repose.agents.event_monitor import _save_config
            _save_config(cfg)
            return {"form_endpoint_added": name, "secret_id": secret_id}

        if verb == "test":
            return self._handle_test("form", args)

        raise ValueError(f"Unknown verb for form: {verb}")

    # ── Status ────────────────────────────────────────────────────────

    def _handle_status(self, verb: str, args: List[str]) -> Any:
        from repose.agents.event_monitor import get_stats
        return get_stats()

    # ── Test Helper ───────────────────────────────────────────────────

    def _handle_test(self, source: str, args: List[str]) -> Any:
        """Process a test event from a fixture file."""
        fixture_path = ""
        i = 0
        while i < len(args):
            if args[i] == "--event-fixture" and i + 1 < len(args):
                fixture_path = args[i + 1]
                i += 2
            else:
                i += 1

        if not fixture_path:
            return {"error": "--event-fixture is required"}

        # Resolve fixture path
        fixtures_dir = os.path.join(
            os.path.dirname(__file__), "..", "fixtures", "event_monitor"
        )
        full_path = os.path.join(fixtures_dir, fixture_path)
        if not os.path.exists(full_path):
            full_path = fixture_path  # Try absolute path
        if not os.path.exists(full_path):
            return {"error": f"Fixture not found: {fixture_path} (tried {full_path})"}

        with open(full_path) as fh:
            payload = json.load(fh)

        event_type = payload.get("type", payload.get("event_type", "unknown"))
        headers = {}
        from repose.agents.event_monitor import process_event
        result = process_event(source, event_type, payload, headers)
        return result


# Also keep the stub for backward compat
class Event_monitorCLIStub(CLIBase):
    """Fallback stub — used when event_monitor worker module is not installed."""
    agent_name = "event_monitor"
    nouns = ["sources", "events", "ingress", "stripe", "github", "form", "status"]

    def handle(self, noun: str, verb: str, args: list):
        if verb in ("list", "history"):
            return []
        if verb == "test":
            return {"agent": "event_monitor", "status": "not_implemented"}
        return {"agent": "event_monitor", "noun": noun, "verb": verb, "status": "ok"}
