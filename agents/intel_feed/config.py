"""Intel_feed Lite agent configuration loader.

Loads intel_feed.yaml, intel_feed_sources.yaml, intel_feed_keywords.yaml,
and intel_feed_sanitization_patterns.yaml from repose/config/intel_feed/.
"""

import logging
import os
from pathlib import Path
import yaml

logger = logging.getLogger(__name__)

# Config directory
_INTEL_FEED_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config" / "intel_feed"

# In-memory caches
_intel_feed_config: dict | None = None
_intel_feed_sources: dict | None = None
_intel_feed_keywords: dict | None = None
_intel_feed_sanitization_patterns: dict | None = None


def _load_yaml(filename: str) -> dict:
    """Load a YAML file from the intel_feed config directory."""
    path = _INTEL_FEED_CONFIG_DIR / filename
    if not path.exists():
        logger.warning("Intel_feed config file not found: %s", path)
        return {}
    with open(path) as fh:
        data = yaml.safe_load(fh) or {}
    logger.info("Loaded intel_feed config: %s", filename)
    return data


def get_intel_feed_config() -> dict:
    """Load and cache intel_feed.yaml."""
    global _intel_feed_config
    if _intel_feed_config is None:
        _intel_feed_config = _load_yaml("intel_feed.yaml")
    return _intel_feed_config


def get_sources() -> list[dict]:
    """Load and cache intel_feed_sources.yaml, returning the sources list."""
    global _intel_feed_sources
    if _intel_feed_sources is None:
        data = _load_yaml("intel_feed_sources.yaml")
        _intel_feed_sources = data.get("sources", [])
    return _intel_feed_sources


def get_egress_allowlist() -> list[str]:
    """Return the egress allowlist from intel_feed_sources.yaml."""
    global _intel_feed_sources
    if _intel_feed_sources is None:
        data = _load_yaml("intel_feed_sources.yaml")
        _intel_feed_sources = data.get("sources", [])
    return _load_yaml("intel_feed_sources.yaml").get("egress_allowlist", [])


def get_keywords() -> list[str]:
    """Load and cache intel_feed_keywords.yaml, returning keyword list."""
    global _intel_feed_keywords
    if _intel_feed_keywords is None:
        data = _load_yaml("intel_feed_keywords.yaml")
        _intel_feed_keywords = data.get("keywords", [])
    return _intel_feed_keywords


def get_sanitization_patterns() -> list[dict]:
    """Load and cache intel_feed_sanitization_patterns.yaml."""
    global _intel_feed_sanitization_patterns
    if _intel_feed_sanitization_patterns is None:
        data = _load_yaml("intel_feed_sanitization_patterns.yaml")
        _intel_feed_sanitization_patterns = data.get("patterns", [])
    return _intel_feed_sanitization_patterns


def get_block_patterns() -> list[str]:
    """Return block-pattern names from intel_feed_sanitization_patterns.yaml.

    These name patterns (from the patterns list) that must cause the whole item
    to be blocked/skipped rather than merely stripped. Empty = block nothing.
    """
    return _load_yaml("intel_feed_sanitization_patterns.yaml").get("block_patterns", [])


def reload_all():
    """Force reload all config caches."""
    global _intel_feed_config, _intel_feed_sources, _intel_feed_keywords, _intel_feed_sanitization_patterns
    _intel_feed_config = None
    _intel_feed_sources = None
    _intel_feed_keywords = None
    _intel_feed_sanitization_patterns = None
