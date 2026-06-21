"""Sanitization layer for Intel_feed Lite.

Strips known prompt injection patterns from title and summary fields
before LLM scoring. Patterns are operator-maintainable in
intel_feed_sanitization_patterns.yaml.
"""

import logging
import re
from typing import Optional

from repose.agents.intel_feed.config import get_sanitization_patterns
from repose.utils.orca import log_system_event

logger = logging.getLogger(__name__)

# Cache compiled patterns
_compiled_patterns: list[dict] | None = None


def _load_patterns() -> list[dict]:
    """Load and compile sanitization patterns."""
    global _compiled_patterns
    if _compiled_patterns is not None:
        return _compiled_patterns

    raw_patterns = get_sanitization_patterns()
    _compiled_patterns = []
    for p in raw_patterns:
        # Support both 'regex' key (my format) and 'pattern' key (other format)
        pattern_str = p.get("regex") or p.get("pattern", "")
        if not pattern_str:
            continue
        try:
            compiled = re.compile(pattern_str)
            _compiled_patterns.append({
                "name": p["name"],
                "regex": compiled,
                "severity": p.get("severity", "medium"),
            })
        except re.error as exc:
            logger.error("Invalid regex in pattern '%s': %s", p.get("name"), exc)
    return _compiled_patterns


def sanitize_text(text: str, field_name: str = "text") -> dict:
    """Sanitize a text string by stripping injection patterns.

    Returns:
        dict with: sanitized, stripped, patterns_matched, error.
    """
    if not text:
        return {"sanitized": text, "stripped": False, "patterns_matched": [], "error": None}

    patterns = _load_patterns()
    stripped = False
    patterns_matched = []
    sanitized = text

    for pattern in patterns:
        try:
            match = pattern["regex"].search(sanitized)
            if match:
                old_len = len(sanitized)
                sanitized = pattern["regex"].sub("[REDACTED]", sanitized)
                patterns_matched.append({
                    "pattern": pattern["name"],
                    "severity": pattern["severity"],
                    "match_preview": match.group()[:80],
                })
                if len(sanitized) != old_len:
                    stripped = True
                logger.info(
                    "Sanitization: pattern '%s' matched in %s field",
                    pattern["name"], field_name,
                )
        except Exception as exc:
            logger.error(
                "Error applying pattern '%s': %s",
                pattern["name"], exc,
            )
            return {
                "sanitized": text,
                "stripped": False,
                "patterns_matched": patterns_matched,
                "error": str(exc),
            }

    return {
        "sanitized": sanitized.strip(),
        "stripped": stripped,
        "patterns_matched": patterns_matched,
        "error": None,
    }


def sanitize_item(item: dict) -> dict:
    """Sanitize title and summary of a signal item.

    Returns:
        dict with: success, sanitized_title, sanitized_summary, title_stripped,
                   summary_stripped, title_patterns, summary_patterns, error.
    """
    from repose.agents.intel_feed.config import get_intel_feed_config
    config = get_intel_feed_config()
    sanitization_config = config.get("sanitization", {})
    enabled = sanitization_config.get("enabled", True)
    on_failure = sanitization_config.get("on_sanitization_failure", "skip_item")

    if not enabled:
        return {
            "success": True,
            "sanitized_title": item.get("title", ""),
            "sanitized_summary": item.get("summary", ""),
            "title_stripped": False,
            "summary_stripped": False,
            "title_patterns": [],
            "summary_patterns": [],
            "error": None,
        }

    title_result = sanitize_text(item.get("title", ""), "title")
    summary_result = sanitize_text(item.get("summary", ""), "summary")

    # Gap 10: block-pattern enforcement. If any matched pattern is named in
    # block_patterns, the whole item is dropped (not just stripped) so it never
    # reaches the LLM scorer.
    from repose.agents.intel_feed.config import get_block_patterns
    block_names = set(get_block_patterns())
    if block_names:
        matched_names = (
            {p["pattern"] for p in title_result["patterns_matched"]}
            | {p["pattern"] for p in summary_result["patterns_matched"]}
        )
        blocked_hits = matched_names & block_names
        if blocked_hits:
            log_system_event(
                namespace="system-events",
                agent="intel_feed",
                message_preview=item.get("title", "")[:100],
                extra={
                    "event_type": "sanitization_blocked",
                    "source_id": item.get("source_id"),
                    "block_patterns": sorted(blocked_hits),
                },
            )
            logger.warning(
                "Sanitization: item blocked by block_patterns %s (source_id=%s)",
                sorted(blocked_hits), item.get("source_id"),
            )
            return {
                "success": False,
                "sanitized_title": "",
                "sanitized_summary": "",
                "title_stripped": title_result["stripped"],
                "summary_stripped": summary_result["stripped"],
                "title_patterns": title_result["patterns_matched"],
                "summary_patterns": summary_result["patterns_matched"],
                "blocked": True,
                "error": None,
            }

    if title_result["error"] or summary_result["error"]:
        error = title_result["error"] or summary_result["error"]
        log_system_event(
            namespace="system-events",
            agent="intel_feed",
            message_preview=item.get("title", "")[:100],
            error=error,
            extra={
                "event_type": "sanitization_failure",
                "source_id": item.get("source_id"),
            },
        )

        if on_failure == "skip_item":
            return {
                "success": False,
                "sanitized_title": "",
                "sanitized_summary": "",
                "title_stripped": False,
                "summary_stripped": False,
                "title_patterns": [],
                "summary_patterns": [],
                "error": error,
            }

    if title_result["stripped"] or summary_result["stripped"]:
        log_system_event(
            namespace="system-events",
            agent="intel_feed",
            message_preview=item.get("title", "")[:100],
            extra={
                "event_type": "sanitization_stripped",
                "source_id": item.get("source_id"),
                "title_patterns": [p["pattern"] for p in title_result["patterns_matched"]],
                "summary_patterns": [p["pattern"] for p in summary_result["patterns_matched"]],
            },
        )

    return {
        "success": True,
        "sanitized_title": title_result["sanitized"],
        "sanitized_summary": summary_result["sanitized"][:500],
        "title_stripped": title_result["stripped"],
        "summary_stripped": summary_result["stripped"],
        "title_patterns": title_result["patterns_matched"],
        "summary_patterns": summary_result["patterns_matched"],
        "error": None,
    }
