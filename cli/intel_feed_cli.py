"""
Intel_feed CLI — repose intel_feed <noun> <verb> [args] [--format json]

Nouns: sources, observations, scan, test, sanitization, status
Verbs: list, enable, disable, add, history, --now, --source

All commands support --format json. Deterministic sort order. Exit codes 0/1/2.
"""
from __future__ import annotations

import json
import logging
import sys
from typing import Any, List

from repose.utils.cli_base import CLIBase, EXIT_SUCCESS, EXIT_ERROR, EXIT_WARNING

logger = logging.getLogger(__name__)


class Intel_feedCLI(CLIBase):
    """Intel_feed CLI — intelligence scout signal pipeline."""

    agent_name = "intel_feed"
    nouns = ["sources", "observations", "scan", "test", "sanitization", "status"]

    def run(self) -> int:
        """Override run to handle non-standard verbs for intel_feed-specific nouns."""
        # Reuse parent's parser logic but with more flexible verbs
        import argparse

        parser = argparse.ArgumentParser(
            prog=f"repose {self.agent_name}",
            description=f"Repose OS — {self.agent_name.upper()} agent CLI",
        )
        parser.add_argument("noun", nargs="?", choices=self.nouns)
        parser.add_argument("verb_or_id", nargs="?", default=None)
        parser.add_argument("--format", choices=["text", "json"], default=None)
        parser.add_argument("--yes", action="store_true")
        # Source management args
        parser.add_argument("--url", default=None)
        parser.add_argument("--class", dest="source_class", type=int, default=None)
        parser.add_argument("--rationale", default=None)
        # Test / scan args
        parser.add_argument("--source", default=None)
        parser.add_argument("--now", action="store_true")
        # Sanitization test
        parser.add_argument("--text", default=None)
        # Observations
        parser.add_argument("--last", default=None)
        parser.add_argument("--surfaced-only", action="store_true")

        parsed = parser.parse_args(self.args)
        self._parsed_args = parsed

        if parsed.format == "json":
            self.use_json = True

        if not parsed.noun:
            parser.print_help()
            return 0

        noun = parsed.noun
        verb = parsed.verb_or_id or ""

        try:
            data = self.handle(noun, verb, parsed)
            formatted = self.format_output(data)
            print(formatted)
            return 0
        except Exception as exc:
            logger.exception(f"CLI error: {exc}")
            print(f"Error: {exc}", file=sys.stderr)
            return 1

    def handle(self, noun: str, verb: str, parsed) -> Any:
        """Route commands to handlers."""

        if noun == "sources":
            return self._handle_sources(verb, parsed)

        if noun == "observations":
            return self._handle_observations(verb, parsed)

        if noun == "scan":
            return self._handle_scan(verb, parsed)

        if noun == "test":
            return self._handle_test(verb, parsed)

        if noun == "sanitization":
            return self._handle_sanitization(verb, parsed)

        if noun == "status":
            return self._handle_status(verb, parsed)

        raise ValueError(f"Unknown noun: {noun}")

    # ── Sources ──────────────────────────────────────────────────────

    def _handle_sources(self, verb: str, parsed) -> Any:
        from repose.agents.intel_feed.config import get_sources, reload_all

        if verb == "list" or verb == "":
            sources = get_sources()
            result = []
            for s in sources:
                result.append({
                    "id": s["id"],
                    "name": s.get("name", ""),
                    "url": s.get("url", ""),
                    "class": s.get("class", 1),
                    "type": s.get("type", "rss"),
                    "enabled": s.get("enabled", True),
                    "fetch_interval_hours": s.get("fetch_interval_hours", 24),
                    "rationale": s.get("rationale", ""),
                })
            result.sort(key=lambda x: (x["class"], x["id"]))
            return result

        target = verb  # source id passed as verb_or_id

        if verb == "add":
            url = getattr(parsed, "url", None)
            source_class = getattr(parsed, "source_class", None)
            rationale = getattr(parsed, "rationale", "") or ""
            if not url:
                return {"error": "--url is required"}
            if source_class not in (1, 2, 3):
                return {"error": "--class must be 1, 2, or 3"}
            return self._add_source(url, source_class, rationale)

        if verb == "enable" or verb == "disable":
            if not target:
                return {"error": "source_id required"}
            return self._toggle_source(target, verb == "enable")

        if verb == "history":
            if not target:
                return {"error": "source_id required"}
            return self._source_history(target)

        if not target:
            return self._handle_sources("list", parsed)

        # Assume verb is a source_id and use list as default
        # (handles 'repose intel_feed sources list' without explicit 'list' word)
        return self._handle_sources("list", parsed)

    def _add_source(self, url: str, source_class: int, rationale: str) -> dict:
        import re
        import yaml
        from pathlib import Path
        from repose.agents.intel_feed.config import get_sources, reload_all

        # Write to the same directory the Intel_feed engine reads from
        # (repose/config/intel_feed — see agents/intel_feed/config.py _INTEL_FEED_CONFIG_DIR).
        # Previously this wrote to repose/config, which the engine never reads,
        # so CLI source edits had no effect until reconciled.
        config_dir = Path(__file__).resolve().parent.parent / "config" / "intel_feed"
        sources_path = config_dir / "intel_feed_sources.yaml"

        # Generate source ID
        domain = url.split("//")[-1].split("/")[0]
        source_id = re.sub(r"[^a-z0-9]", "_", domain.lower())[:40]
        name = domain

        new_source = {
            "id": source_id,
            "name": name,
            "url": url,
            "class": source_class,
            "type": "rss",
            "enabled": True,
            "fetch_interval_hours": 24,
            "rationale": rationale,
        }

        # Read existing
        with open(sources_path) as fh:
            data = yaml.safe_load(fh) or {}
        data.setdefault("sources", []).append(new_source)

        with open(sources_path, "w") as fh:
            yaml.safe_dump(data, fh, default_flow_style=False, sort_keys=False)

        reload_all()
        return new_source

    def _toggle_source(self, source_id: str, enable: bool) -> dict:
        import yaml
        from pathlib import Path
        from repose.agents.intel_feed.config import reload_all

        # Write to the same directory the Intel_feed engine reads from
        # (repose/config/intel_feed — see agents/intel_feed/config.py _INTEL_FEED_CONFIG_DIR).
        # Previously this wrote to repose/config, which the engine never reads,
        # so CLI source edits had no effect until reconciled.
        config_dir = Path(__file__).resolve().parent.parent / "config" / "intel_feed"
        sources_path = config_dir / "intel_feed_sources.yaml"

        with open(sources_path) as fh:
            data = yaml.safe_load(fh) or {}

        found = False
        for s in data.get("sources", []):
            if s["id"] == source_id:
                s["enabled"] = enable
                found = True

        if not found:
            return {"error": f"Source not found: {source_id}"}

        with open(sources_path, "w") as fh:
            yaml.safe_dump(data, fh, default_flow_style=False, sort_keys=False)

        reload_all()
        return {"source_id": source_id, "enabled": enable}

    def _source_history(self, source_id: str) -> list[dict]:
        return [{"source_id": source_id, "history": "No change history available"}]

    # ── Observations ──────────────────────────────────────────────────

    def _handle_observations(self, verb: str, parsed) -> Any:
        from repose.agents.intel_feed.engine import get_observations

        if verb not in ("list", ""):
            raise ValueError(f"Unknown verb for observations: {verb}")

        last_val = getattr(parsed, "last", None)
        surfaced_only = getattr(parsed, "surfaced_only", False)

        last_days = 7
        if last_val:
            try:
                last_days = int(last_val.replace("d", ""))
            except ValueError:
                pass

        return get_observations(last_days, surfaced_only)

    # ── Scan ──────────────────────────────────────────────────────────

    def _handle_scan(self, verb: str, parsed) -> Any:
        from repose.agents.intel_feed.engine import run_scan, reset_engine

        is_now = getattr(parsed, "now", False) or verb == "now" or verb == "--now"
        source_id = getattr(parsed, "source", None)

        if not is_now:
            return {"error": "Use 'repose intel_feed scan --now' to trigger a scan"}

        if source_id:
            # Single source scan
            from repose.agents.intel_feed.ingestion import fetch_all_sources
            from repose.agents.intel_feed.config import get_sources
            return {"scan_single_source": source_id, "status": "not implemented for single source"}

        result = run_scan()
        return {
            "scan_id": result["scan_id"],
            "items_fetched": result["items_fetched"],
            "items_scored": result["items_scored"],
            "items_surfaced": result["items_surfaced"],
            "warmup_active": result["warmup_active"],
            "cost_estimate": result["cost_estimate"],
        }

    # ── Test ──────────────────────────────────────────────────────────

    def _handle_test(self, verb: str, parsed) -> Any:
        from repose.agents.intel_feed.engine import run_test

        source_id = getattr(parsed, "source", None) or verb

        if not source_id or source_id == "test":
            return {"error": "--source <source_id> required (e.g., --source arxiv_cs_ai)"}

        result = run_test(source_id)
        if result is None:
            return {"error": f"Could not test source: {source_id}"}
        return result

    # ── Sanitization ──────────────────────────────────────────────────

    def _handle_sanitization(self, verb: str, parsed) -> Any:
        from repose.agents.intel_feed.sanitization import sanitize_text

        text = getattr(parsed, "text", None)

        if not text:
            return {"error": "--text is required (e.g., --text 'IGNORE PREVIOUS INSTRUCTIONS')"}

        result = sanitize_text(text, "test_text")
        return {
            "original": text,
            "sanitized": result["sanitized"],
            "was_modified": result["stripped"],
            "patterns_matched": [m["pattern"] for m in result.get("patterns_matched", [])],
        }

    # ── Status ────────────────────────────────────────────────────────

    def _handle_status(self, verb: str, parsed) -> Any:
        from repose.agents.intel_feed.engine import get_status

        if verb not in ("", "status", "list"):
            raise ValueError(f"Unknown verb for status: {verb}")

        return get_status()
