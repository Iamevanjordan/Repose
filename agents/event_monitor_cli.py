"""
Event_monitor CLI — Repose OS Event Watcher CLI

Follows the standard verb pattern:
    repose event_monitor <noun> <verb> [args] [--format json]

All commands support --format json for deterministic, machine-readable output.
Exit codes: 0 = success, 1 = error, 2 = warning.

Commands (Section 11 of Event_monitor MVP Brief v3):
  ingress setup/status
  sources list/enable/disable/history
  stripe setup
  github setup / set-repos
  form add-endpoint
  test --source <source> --event-fixture <path>
  restart
  status
  events list [--lane] [--source] [--last]
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

# Add parent to path for repose imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from repose.utils.cli_base import CLIBase, _supports_color, _format_green, _format_yellow, _format_red

logger = logging.getLogger(__name__)

# ── Fixture base path ────────────────────────────────────────────────────
FIXTURES_BASE = Path(__file__).resolve().parent.parent / "fixtures" / "event_monitor"

# NOTE (RPOSE-FIND6): test mode is NO LONGER enabled as a module-level import
# side effect. Importing this CLI used to call
# os.environ.setdefault("EVENT_MONITOR_TEST_MODE", "true"), which globally
# routed the live classifier to its offline heuristic for the entire `repose`
# process — so `repose event_monitor` silently diverged from live daemon
# behavior. Test mode is now OPT-IN: the offline `test` subcommand enables it
# for its own run only, and an explicit `--test-mode` flag enables it for other
# commands when a user asks. EVENT_MONITOR_TEST_MODE only routes the classifier
# to its local heuristic (no LLM API call); it never affects signature
# verification (webhook HMAC always runs and fails closed — see process_event).


class Event_monitorCLI(CLIBase):
    """Event_monitor agent CLI — full implementation per Section 11 of Event_monitor MVP Brief v3."""

    agent_name = "event_monitor"
    nouns = [
        "sources", "events", "ingress", "stripe", "github", "form", "status"
    ]

    def handle(self, noun: str, verb: str, args: List[str]) -> Any:
        """Route CLI command to the appropriate handler."""
        if noun == "ingress":
            return self._handle_ingress(verb, args)
        if noun == "sources":
            return self._handle_sources(verb, args)
        if noun == "events":
            return self._handle_events(verb, args)
        if noun == "stripe":
            return self._handle_stripe(verb, args)
        if noun == "github":
            return self._handle_github(verb, args)
        if noun == "form":
            return self._handle_form(verb, args)
        if noun == "status":
            return self._status()
        raise ValueError(f"Unknown noun '{noun}' for event_monitor agent")

    # ──── Ingress ────────────────────────────────────────────────────

    def _handle_ingress(self, verb: str, args: List[str]) -> dict:
        if verb == "setup":
            return self._ingress_setup()
        elif verb in ("status", "list"):
            return self._ingress_status()
        raise ValueError(f"Unknown ingress verb: {verb}")

    def _ingress_setup(self) -> dict:
        from repose.agents.event_monitor import get_config
        cfg = get_config()
        tunnel_name = cfg.get("webhook_ingress", {}).get("tunnel_name", "repose-event_monitor")

        use_color = _supports_color()
        print(_format_green("\n  Event_monitor Ingress Setup\n", use_color))
        print(f"  Provider: cloudflared (LOCKED)")
        print(f"  Tunnel name: {tunnel_name}")
        # Replace with your cloudflared webhook domain.
        print(f"  Public URL: https://your-webhook-domain.example.com")
        print()

        import subprocess
        cloudflared_ok = False
        try:
            result = subprocess.run(["which", "cloudflared"], capture_output=True, text=True, timeout=5)
            cloudflared_ok = result.returncode == 0
        except Exception:
            pass

        status = "simulated" if not cloudflared_ok else "installed"
        print(_format_green(f"  Ingress setup recorded. Status: {status}", use_color))

        return {
            "provider": "cloudflared",
            "tunnel_name": tunnel_name,
            "public_url": "https://your-webhook-domain.example.com",
            "listen_port": cfg.get("webhook_ingress", {}).get("listen_port", 8080),
            "status": status,
        }

    def _ingress_status(self) -> dict:
        from repose.agents.event_monitor import get_config
        cfg = get_config()
        ingress = cfg.get("webhook_ingress", {})
        return {
            "type": ingress.get("type", "cloudflared"),
            "tunnel_name": ingress.get("tunnel_name", ""),
            "public_url": "https://your-webhook-domain.example.com",
            "listen_port": ingress.get("listen_port", 8080),
            "health_endpoint": "https://your-webhook-domain.example.com/health",
            "reachable": "simulated",
        }

    # ──── Sources ────────────────────────────────────────────────────

    def _handle_sources(self, verb: str, args: List[str]) -> Any:
        if verb == "list":
            return self._sources_list()
        elif verb == "enable":
            return self._sources_toggle(args, enabled=True)
        elif verb == "disable":
            return self._sources_toggle(args, enabled=False)
        elif verb == "history":
            return []
        raise ValueError(f"Unknown sources verb: {verb}")

    def _sources_list(self) -> list[dict]:
        from repose.agents.event_monitor import get_config
        cfg = get_config()
        sources = cfg.get("sources", {})
        result = []
        for name, scfg in sorted(sources.items()):
            if name == "gmail":
                continue
            result.append({
                "source": name,
                "enabled": scfg.get("enabled", False),
                "endpoint": scfg.get("endpoint", ""),
                "dedup_strategy": scfg.get("dedup_strategy", ""),
                "dedup_ttl_seconds": scfg.get("dedup_ttl_seconds", 0),
            })
        return result

    def _sources_toggle(self, args: List[str], enabled: bool) -> dict:
        source_name = args[0] if args else ""
        if not source_name:
            raise ValueError("Source name required")
        from repose.agents.event_monitor import get_config
        cfg = get_config()
        if source_name not in cfg.get("sources", {}):
            raise ValueError(f"Unknown source: {source_name}")
        if source_name == "gmail":
            raise ValueError("Gmail is explicitly excluded from MVP")
        cfg["sources"][source_name]["enabled"] = enabled
        return {
            "source": source_name,
            "enabled": enabled,
            "verb": "enable" if enabled else "disable",
        }

    # ──── Events ─────────────────────────────────────────────────────

    def _handle_events(self, verb: str, args: List[str]) -> Any:
        if verb == "list":
            return self._events_list(args)
        raise ValueError(f"Unknown events verb: {verb}")

    def _events_list(self, args: List[str]) -> list[dict]:
        lane = None
        source = None
        last_hours = None
        for i, arg in enumerate(args):
            if arg == "--lane" and i + 1 < len(args):
                lane = args[i + 1]
            elif arg == "--source" and i + 1 < len(args):
                source = args[i + 1]
            elif arg == "--last" and i + 1 < len(args):
                try:
                    last_hours_str = args[i + 1].rstrip("h")
                    last_hours = float(last_hours_str)
                except ValueError:
                    pass

        from repose.agents.event_monitor import get_events
        return get_events(lane=lane, source=source, last_hours=last_hours)

    # ──── Status ─────────────────────────────────────────────────────

    def _status(self) -> dict:
        from repose.agents.event_monitor import get_status
        return get_status()

    # ──── Stripe Setup ───────────────────────────────────────────────

    def _handle_stripe(self, verb: str, args: List[str]) -> dict:
        if verb == "setup":
            return self._stripe_setup()
        raise ValueError(f"Unknown stripe verb: {verb}")

    def _stripe_setup(self) -> dict:
        use_color = _supports_color()
        print(_format_green("\n  Event_monitor Stripe Setup\n", use_color))
        print("  Step 1: Create webhook endpoint in Stripe Dashboard")
        print("    Path: Developers > Webhooks > Add endpoint")
        print("    Endpoint URL: https://your-webhook-domain.example.com/webhooks/stripe")
        print()

        import getpass
        secret = ""
        try:
            secret = getpass.getpass("  Step 2: Paste Stripe signing secret (whsec_...): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Setup aborted.")
            return {"status": "aborted"}

        if not secret or not secret.startswith("whsec_"):
            print(_format_red("  Invalid signing secret format.", use_color))
            return {"status": "failed", "reason": "invalid_secret_format"}

        print(_format_green("  Secret accepted.", use_color))

        # Persist the signing secret to Bitwarden Secrets Manager — the only
        # secrets layer (RPOSE-008). Never write secrets into os.environ: a
        # process-environment secret leaks to every child process and is
        # readable via /proc; Bitwarden is the single source of truth.
        try:
            from repose.utils.bitwarden import store_secret
            store_secret("repose-stripe-signing-secret", secret)
        except Exception as exc:
            print(_format_red(f"  Bitwarden store failed: {exc}", use_color))
            return {"status": "failed", "reason": "bitwarden_store_failed"}

        # Verify signature
        print("\n  Step 3: Verify signature...")
        import hmac, hashlib, time
        test_payload = json.dumps({"type": "payment_intent.created", "id": "evt_test"}).encode("utf-8")
        timestamp = str(int(time.time()))
        signed = f"{timestamp}.{json.dumps({'type':'payment_intent.created','id':'evt_test'})}"
        test_sig = hmac.new(secret.encode(), signed.encode(), hashlib.sha256).hexdigest()
        test_header = f"t={timestamp},v1={test_sig}"

        from repose.agents.event_monitor import verify_stripe_signature
        verified = verify_stripe_signature(test_payload, test_header, secret)

        if verified:
            print(_format_green("  Signature verification PASSED", use_color))
        else:
            print(_format_red("  Signature verification FAILED", use_color))
            return {"status": "failed", "reason": "signature_verification_failed"}

        from repose.agents.event_monitor import get_config
        cfg = get_config()
        if "sources" in cfg and "stripe" in cfg["sources"]:
            cfg["sources"]["stripe"]["enabled"] = True

        print(_format_green("\n  Stripe webhook setup complete.", use_color))
        return {
            "status": "complete",
            "source": "stripe",
            "endpoint": "/webhooks/stripe",
            "enabled": True,
            "signature_verified": verified,
        }

    # ──── GitHub Setup ───────────────────────────────────────────────

    def _handle_github(self, verb: str, args: List[str]) -> Any:
        if verb == "setup":
            return self._github_setup()
        elif verb == "set-repos":
            return self._github_set_repos(args)
        raise ValueError(f"Unknown github verb: {verb}")

    def _github_setup(self) -> dict:
        use_color = _supports_color()
        print(_format_green("\n  Event_monitor GitHub Setup\n", use_color))
        print("  Step 1: Add webhook in GitHub repository settings")
        print("    Path: Settings > Webhooks > Add webhook")
        print("    Payload URL: https://your-webhook-domain.example.com/webhooks/github")
        print("    Secret: (generate one, enter below)")
        print()

        import getpass
        secret = ""
        try:
            secret = getpass.getpass("  Step 2: Paste GitHub webhook secret: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Setup aborted.")
            return {"status": "aborted"}

        if not secret:
            print(_format_red("  Secret is required.", use_color))
            return {"status": "failed"}

        # Persist to Bitwarden Secrets Manager — the only secrets layer
        # (RPOSE-008). Never stash secrets in os.environ.
        try:
            from repose.utils.bitwarden import store_secret
            store_secret("repose-github-webhook-secret", secret)
        except Exception as exc:
            print(_format_red(f"  Bitwarden store failed: {exc}", use_color))
            return {"status": "failed", "reason": "bitwarden_store_failed"}
        print(_format_green("  Secret stored.", use_color))

        # Verify
        import hmac, hashlib
        test_payload = b'{"ref":"refs/heads/main","zen":"test"}'
        test_sig = "sha256=" + hmac.new(secret.encode(), test_payload, hashlib.sha256).hexdigest()

        from repose.agents.event_monitor import verify_github_signature
        verified = verify_github_signature(test_payload, test_sig, secret)

        if verified:
            print(_format_green("  Signature verification PASSED", use_color))
        else:
            print(_format_red("  Signature verification FAILED", use_color))
            return {"status": "failed", "reason": "signature_verification_failed"}

        print(_format_green("\n  GitHub webhook setup complete.", use_color))
        return {
            "status": "complete",
            "source": "github",
            "endpoint": "/webhooks/github",
            "enabled": True,
        }

    def _github_set_repos(self, args: List[str]) -> dict:
        pattern = args[0] if args else "*"
        from repose.agents.event_monitor import get_config
        cfg = get_config()
        if "sources" in cfg and "github" in cfg["sources"]:
            cfg["sources"]["github"]["monitored_repos"] = [pattern]
        return {"source": "github", "monitored_repos": [pattern]}

    # ──── Form ───────────────────────────────────────────────────────

    def _handle_form(self, verb: str, args: List[str]) -> dict:
        if verb == "add-endpoint":
            return self._form_add_endpoint(args)
        raise ValueError(f"Unknown form verb: {verb}")

    def _form_add_endpoint(self, args: List[str]) -> dict:
        name = ""
        secret_id = ""
        i = 0
        while i < len(args):
            if args[i].startswith("--name"):
                if "=" in args[i]:
                    name = args[i].split("=", 1)[1]
                elif i + 1 < len(args):
                    name = args[i + 1]; i += 1
            elif args[i].startswith("--secret-id"):
                if "=" in args[i]:
                    secret_id = args[i].split("=", 1)[1]
                elif i + 1 < len(args):
                    secret_id = args[i + 1]; i += 1
            i += 1

        if not name:
            raise ValueError("--name is required")

        from repose.agents.event_monitor import get_config
        cfg = get_config()
        if "sources" in cfg and "form" in cfg["sources"]:
            endpoints = cfg["sources"]["form"].get("endpoints", [])
            endpoints.append({"name": name, "secret_id": secret_id or f"bitwarden:repose-form-{name}-secret"})
            cfg["sources"]["form"]["endpoints"] = endpoints

        return {"source": "form", "endpoint_name": name, "secret_id": secret_id, "status": "added"}

    # ──── Run method ─────────────────────────────────────────────────

    def run(self) -> int:
        parser = argparse.ArgumentParser(
            prog=f"repose {self.agent_name}",
            description=f"Repose OS — {self.agent_name.upper()} agent CLI",
        )
        parser.add_argument("noun_or_cmd", nargs="?", help="Noun or command")
        parser.add_argument("verb", nargs="?", help="Action to perform")
        parser.add_argument("extra_args", nargs="*", help="Additional args")
        parser.add_argument("--format", choices=["text", "json"], default=None)
        parser.add_argument("--source", default=None)
        parser.add_argument("--lane", default=None)
        parser.add_argument("--last", default=None)
        parser.add_argument("--event-fixture", default=None)
        parser.add_argument("--yes", action="store_true")
        parser.add_argument(
            "--test-mode",
            action="store_true",
            help="Route the classifier to the offline heuristic (no LLM call). "
                 "Opt-in only; live commands use real classification by default.",
        )

        parsed = parser.parse_args(self.args)
        self._parsed_args = parsed

        if parsed.format == "json":
            self.use_json = True

        # Opt-in test mode for live commands (never a module-level default).
        if parsed.test_mode:
            os.environ["EVENT_MONITOR_TEST_MODE"] = "true"

        # Special: 'test' as standalone command
        if parsed.noun_or_cmd == "test":
            return self._handle_test(parsed)

        if not parsed.noun_or_cmd or not parsed.verb:
            parser.print_help()
            return 0

        noun = parsed.noun_or_cmd
        verb = parsed.verb

        try:
            extra_args = list(parsed.extra_args) if parsed.extra_args else []
            if parsed.source and noun == "events":
                extra_args.extend(["--source", parsed.source])
            if parsed.lane and noun == "events":
                extra_args.extend(["--lane", parsed.lane])
            if parsed.last and noun == "events":
                extra_args.extend(["--last", parsed.last])

            data = self.handle(noun, verb, extra_args)
            formatted = self.format_output(data)
            print(formatted)
            return 0
        except Exception as exc:
            logger.exception(f"CLI error: {exc}")
            print(f"Error: {exc}", file=sys.stderr)
            return 1

    def _handle_test(self, parsed) -> int:
        # The `test` subcommand runs offline against a local fixture, so route
        # the classifier to its heuristic for this run only. This is scoped to
        # the test path — it never globally toggles live classification.
        os.environ["EVENT_MONITOR_TEST_MODE"] = "true"

        source = parsed.source
        fixture = parsed.event_fixture

        if not source or not fixture:
            print("Usage: repose event_monitor test --source <source> --event-fixture <path>")
            return 1

        valid_sources = {"stripe", "github", "form"}
        if source not in valid_sources:
            print(f"Error: Unknown source '{source}'. Valid: {', '.join(sorted(valid_sources))}")
            return 1

        fixture_path = Path(fixture)
        if not fixture_path.is_absolute():
            fixture_path = FIXTURES_BASE / fixture

        if not fixture_path.exists():
            print(f"Error: Fixture file not found: {fixture_path}")
            return 1

        try:
            with open(fixture_path) as f:
                payload = json.load(f)
        except json.JSONDecodeError as exc:
            print(f"Error: Invalid JSON: {exc}")
            return 1

        from repose.agents.event_monitor import process_event

        event_record = process_event(
            source=source,
            payload=payload,
            bypass_signature=True,
        )

        if self.use_json:
            print(json.dumps(event_record, indent=2, sort_keys=True, default=str))
        else:
            use_color = _supports_color()
            print()
            if event_record.get("discarded") or event_record.get("status") == "rejected":
                print(_format_yellow(
                    f"  Event REJECTED: {event_record.get('reason', event_record.get('discard_reason', 'unknown'))}",
                    use_color,
                ))
            else:
                lane = event_record.get("lane", "unknown").upper()
                print(_format_green(f"  Event PROCESSED -> {lane}", use_color))
            print(f"  Source:      {event_record.get('source', 'N/A')}")
            print(f"  Event type:  {event_record.get('source_event_type', 'N/A')}")
            print(f"  Lane:        {event_record.get('lane', 'N/A')}")
            print(f"  Confidence:  {event_record.get('classifier_confidence', 'N/A')}")
            print(f"  Model:       {event_record.get('classifier_model', 'N/A')}")
            print(f"  Reasoning:   {event_record.get('classifier_reasoning', 'N/A')}")
            print()

        return 0
