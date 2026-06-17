"""RSS source ingestion engine for Intel_feed Lite.

Fetches RSS feeds from configured sources, parses items,
tracks last-fetch timestamps to avoid duplicates.
"""

import hashlib
import json
import logging
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import feedparser

from repose.agents.intel_feed.config import get_sources, get_egress_allowlist

logger = logging.getLogger(__name__)

# In-memory last-fetch timestamps per source
_last_fetch: dict[str, float] = {}

# In-memory archive of fetched item IDs (dedup)
_fetched_ids: set[str] = set()

# Timestamp file path
_TS_FILE = Path(__file__).resolve().parent.parent.parent.parent / ".intel_feed_last_fetch.json"


def _load_timestamps():
    """Load last-fetch timestamps from disk."""
    global _last_fetch
    if _TS_FILE.exists():
        try:
            _last_fetch = json.loads(_TS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            _last_fetch = {}


def _save_timestamps():
    """Save last-fetch timestamps to disk."""
    try:
        _TS_FILE.write_text(json.dumps(_last_fetch, indent=2))
    except OSError as exc:
        logger.warning("Failed to save timestamps: %s", exc)


def _generate_signal_id(source_id: str, title: str, url: str) -> str:
    """Generate a deterministic signal_id from source + title + url."""
    raw = f"{source_id}|{title}|{url}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def _parse_rss_feed(url: str, source_id: str) -> list[dict]:
    """Parse an RSS feed and return a list of signal items.

    Returns items with: title, summary, url, published, source_id.
    """
    items = []
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Repose-OS-Intel_feed/1.0"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        logger.error("Failed to fetch %s: %s", url, exc)
        return items

    feed = feedparser.parse(raw)
    for entry in feed.entries:
        title = entry.get("title", "").strip()
        summary = entry.get("summary", entry.get("description", "")).strip()
        item_url = entry.get("link", "")
        published_raw = entry.get("published", entry.get("updated", ""))

        # Parse published date
        published = None
        if published_raw:
            try:
                from email.utils import parsedate_to_datetime
                published = parsedate_to_datetime(published_raw)
            except Exception:
                pass

        if not title:
            continue

        item = {
            "title": title,
            "summary": summary[:1000] if summary else "",
            "url": item_url,
            "published": published.isoformat() if published else "",
            "source_id": source_id,
        }
        items.append(item)

    return items


def _egress_allowed(url: str) -> bool:
    """Check a URL host against the operator egress allowlist (RPOSE Gap 10).

    Empty allowlist = allow all (backward compatible). Otherwise the host must
    equal an allowlist entry or be a subdomain of one. Blocks SSRF / exfil to
    hosts the operator never approved.
    """
    from urllib.parse import urlparse
    allow = get_egress_allowlist()
    if not allow:
        return True
    host = urlparse(url).netloc.lower()
    return any(host == a.lower() or host.endswith("." + a.lower()) for a in allow)


def fetch_source(source: dict) -> list[dict]:
    """Fetch new items from a single configured source.

    Args:
        source: Source dict from intel_feed_sources.yaml.

    Returns:
        List of new signal item dicts (not yet archived).
    """
    source_id = source["id"]
    url = source.get("url", "")
    fetch_interval = source.get("fetch_interval_hours", 24)

    if not source.get("enabled", True):
        logger.info("Source %s is disabled, skipping", source_id)
        return []

    if not url:
        logger.warning("Source %s has no URL", source_id)
        return []

    if not _egress_allowed(url):
        logger.error(
            "Source %s blocked: host not in egress allowlist (%s)", source_id, url,
        )
        return []

    # Check if we need to fetch based on interval
    now = time.time()
    last = _last_fetch.get(source_id, 0)
    if now - last < fetch_interval * 3600:
        logger.info("Source %s was fetched recently, skipping", source_id)
        return []

    logger.info("Fetching source: %s (%s)", source_id, url)
    raw_items = _parse_rss_feed(url, source_id)

    # Dedup against previously fetched
    new_items = []
    for item in raw_items:
        sig_id = _generate_signal_id(source_id, item["title"], item["url"])
        if sig_id in _fetched_ids:
            continue
        _fetched_ids.add(sig_id)
        new_items.append(item)

    # Update last-fetch timestamp
    _last_fetch[source_id] = now
    _save_timestamps()

    logger.info("Source %s: fetched %d items, %d new", source_id, len(raw_items), len(new_items))
    return new_items


def fetch_all_sources() -> list[dict]:
    """Fetch new items from all enabled, due sources.

    Returns:
        Combined list of new signal items from all sources.
    """
    _load_timestamps()
    sources = get_sources()
    all_items = []
    for source in sources:
        try:
            items = fetch_source(source)
            all_items.extend(items)
        except Exception as exc:
            logger.exception("Error fetching source %s: %s", source.get("id", "?"), exc)
    return all_items


def test_fetch_source(source_id: str) -> dict | None:
    """Fetch ONE item from a source for testing. No timestamp side effects.

    Args:
        source_id: Source ID to fetch from.

    Returns:
        Single item dict or None if no items found.
    """
    sources = get_sources()
    source = next((s for s in sources if s["id"] == source_id), None)
    if not source:
        logger.error("Source not found: %s", source_id)
        return None

    if not _egress_allowed(source["url"]):
        logger.error(
            "Source %s blocked: host not in egress allowlist (%s)", source_id, source["url"],
        )
        return None

    raw_items = _parse_rss_feed(source["url"], source_id)
    if not raw_items:
        return None

    # Return first item, adding source_class and source_id
    item = raw_items[0]
    item["source_class"] = source.get("class", 1)
    return item


def reset_fetch_state():
    """Reset all fetch timestamps and seen IDs (for testing)."""
    global _last_fetch, _fetched_ids
    _last_fetch = {}
    _fetched_ids = set()
    if _TS_FILE.exists():
        _TS_FILE.unlink(missing_ok=True)
