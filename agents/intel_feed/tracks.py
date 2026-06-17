"""Active tracks context vector generation for Intel_feed Lite.

Generates short context strings from Chronogram business-state that
inform LLM scoring. Tracks regenerate once per scan (cache_per_scan).
"""

import logging
from repose.agents.intel_feed.config import get_intel_feed_config

logger = logging.getLogger(__name__)

# Per-scan cache
_cached_tracks: list[str] | None = None
_cache_scan_id: str | None = None


def generate_active_tracks() -> list[str]:
    """Generate active tracks from Chronogram business-state.

    For MVP, returns a default set of tracks derived from config query.
    In production, this queries Chronogram for active projects, blockers,
    and current focus areas.

    Returns:
        List of active track strings.
    """
    config = get_intel_feed_config()
    tracks_config = config.get("active_tracks", {})
    max_records = tracks_config.get("max_records", 10)

    # For MVP: return sensible defaults representing core focus areas
    # In production, these would come from Chronogram business-state
    default_tracks = [
        "Multi-agent system architecture and orchestration",
        "LLM safety, alignment, and evaluation",
        "Security research — vulnerability detection and mitigation",
        "AI research signal detection and intelligence gathering",
        "Repose OS infrastructure and agent operations framework",
        "Open-source AI tooling and developer experience",
        "Grant and funding opportunity identification",
        "Neural network architectures and training methodologies",
        "Autonomous agent capabilities and benchmarks",
        "Cryptography and privacy-preserving machine learning",
    ]

    tracks = default_tracks[:max_records]
    logger.info("Generated %d active tracks", len(tracks))
    return tracks


def get_or_generate_active_tracks(scan_id: str) -> list[str]:
    """Get active tracks, regenerating if cache_per_scan requires fresh tracks.

    Args:
        scan_id: Unique identifier for the current scan.

    Returns:
        List of active track strings.
    """
    global _cached_tracks, _cache_scan_id

    config = get_intel_feed_config()
    tracks_config = config.get("active_tracks", {})
    cache_per_scan = tracks_config.get("cache_per_scan", True)

    if not cache_per_scan or _cache_scan_id != scan_id:
        _cached_tracks = generate_active_tracks()
        _cache_scan_id = scan_id
        logger.info("Active tracks regenerated for scan %s", scan_id)
    else:
        logger.info("Using cached active tracks for scan %s", scan_id)

    return _cached_tracks


def reset_tracks_cache():
    """Reset active tracks cache (for testing)."""
    global _cached_tracks, _cache_scan_id
    _cached_tracks = None
    _cache_scan_id = None
