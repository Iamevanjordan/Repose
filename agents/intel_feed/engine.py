"""Intel_feed Lite scan orchestration engine.

The main scan pipeline.
"""

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from repose.agents.intel_feed.config import (
    get_intel_feed_config,
    get_sources,
    reload_all,
)
from repose.agents.intel_feed.ingestion import fetch_all_sources, test_fetch_source, reset_fetch_state
from repose.agents.intel_feed.sanitization import sanitize_item
from repose.agents.intel_feed.tracks import get_or_generate_active_tracks, reset_tracks_cache
from repose.agents.intel_feed.gates import evaluate_all_gates
from repose.agents.intel_feed.scoring import (
    is_warmup_active, get_warmup_max_surfaces, reset_warmup,
    _add_to_archive_cache, _archive_cache,
)
from repose.utils.orca import log_system_event
from repose.utils.telegram_router import route_message

logger = logging.getLogger(__name__)

# In-memory scan state
_last_scan_time: float | None = None
_next_scan_time: float | None = None
_total_scans: int = 0
_total_scored: int = 0
_total_surfaced: int = 0
_daily_cost_estimate: float = 0.0
_first_startup_done: bool = False

# In-memory archive for observations
_archive_records: list[dict] = []

# Preload existing records from persistent storage
try:
    from repose.agents.intel_feed.archiver import _read_local_archive
    _archive_records = _read_local_archive(limit=5000)
except Exception:
    pass


def _archive_signal(signal: dict):
    """Persist a scored signal.

    Durable cross-agent persistence goes to the shared ORCA
    (intel_feed-archive namespace) as the PRIMARY path so other agents can
    recall surfaced signals. The in-memory ``_archive_records`` list and the
    scoring novelty cache are kept for fast in-process reads (observations,
    novelty scoring). Local JSONL is now a FALLBACK only — written solely when
    ORCA is unreachable, so per-process restarts still have something to
    rebuild from for debugging."""
    global _archive_records
    _archive_records.append(signal)
    _add_to_archive_cache(signal)

    namespace = (
        get_intel_feed_config().get("chronogram", {}).get("archive_namespace", "intel_feed-archive")
    )
    try:
        from repose.utils.orca import store_artifact
        store_artifact(
            namespace=namespace,
            content=json.dumps(signal, default=str),
            metadata={
                "source": "intel_feed",
                "type_hint": "episodic",
                "source_id": signal.get("signal_id"),
                "intel_source": signal.get("source_id"),
                "surfaced": signal.get("surfaced", False),
                "all_gates_passed": signal.get("all_gates_passed", False),
            },
        )
    except Exception as exc:
        # ORCA unreachable — fall back to local JSONL for durability.
        logger.warning(
            "ORCA archive write failed for signal %s; local JSONL fallback: %s",
            signal.get("signal_id", "?"), exc,
        )
        try:
            from repose.agents.intel_feed.archiver import _write_to_local_archive
            _write_to_local_archive(signal)
        except Exception:
            pass

    # Keep bounded
    if len(_archive_records) > 5000:
        _archive_records = _archive_records[-2500:]


def _build_external_signal(
    item: dict,
    source: dict,
    sanitization_result: dict,
    gates_result: dict,
    active_tracks: list[str],
    warmup_active: bool,
    surfaced: bool = False,
) -> dict:
    """Build an external_signal record per schema (Section 6)."""
    signal_id = str(uuid.uuid4())[:8]
    now = datetime.now(timezone.utc).isoformat()

    return {
        "signal_id": signal_id,
        "schema_version": "1.0",
        "source_id": item.get("source_id", ""),
        "source_class": source.get("class", 1),
        "fetched_at": now,
        "title": sanitization_result.get("sanitized_title", ""),
        "summary": sanitization_result.get("sanitized_summary", ""),
        "url": item.get("url", ""),
        "gate_keyword": gates_result.get("gate_keyword", False),
        "gate_llm_score": gates_result.get("gate_llm_score", 0.0),
        "gate_novelty_score": gates_result.get("gate_novelty_score", 0.0),
        "all_gates_passed": gates_result.get("all_gates_passed", False),
        "surfaced": surfaced,
        "surfaced_at": now if surfaced else None,
        "active_tracks_snapshot": active_tracks,
        "instrument_snapshot_ref": None,
        "warmup_mode": warmup_active,
    }


def _source_class_emoji(source_class: int) -> str:
    emojis = {1: "🔬", 2: "📡", 3: "💡"}
    return emojis.get(source_class, "📎")


def _build_telegram_message(signal: dict, source: dict) -> str:
    emoji = _source_class_emoji(signal["source_class"])
    return (
        f"INTEL_FEED · {emoji} {signal['source_id']}\n"
        f"{signal['title']}\n"
        f"Relevance: {signal['gate_llm_score']} · "
        f"Novelty: {signal['gate_novelty_score']}\n"
        f"{signal['url']}"
    )


def _estimate_cost(items_scored: int, model_used: str) -> float:
    if model_used == "haiku" or model_used == "heuristic":
        per_item = 800/1_000_000 * 0.25 + 100/1_000_000 * 1.25
    elif model_used == "sonnet":
        per_item = 800/1_000_000 * 3.00 + 100/1_000_000 * 15.00
    else:
        per_item = 0.0005
    return items_scored * per_item


def run_scan() -> dict:
    """Execute a full Intel_feed Lite scan pipeline."""
    global _last_scan_time, _next_scan_time, _total_scans
    global _total_scored, _total_surfaced, _daily_cost_estimate, _first_startup_done

    scan_id = str(uuid.uuid4())[:8]
    scan_start = time.time()
    logger.info("=== Intel_feed scan %s starting ===", scan_id)

    config = get_intel_feed_config()
    sources = get_sources()

    # Check warmup (returns dict from scoring.py)
    warmup_info = is_warmup_active()
    warmup_active = warmup_info.get("active", False)
    max_surfaces = get_warmup_max_surfaces() if warmup_active else 99999

    # Check for first startup
    if not _first_startup_done:
        _first_startup_done = True
        if warmup_active:
            log_system_event(
                namespace="system-events",
                agent="intel_feed",
                message_preview="Intel_feed cold start warmup initiated",
                extra={
                    "event_type": "cold_start_initiated",
                    "warmup_days_remaining": warmup_info.get("days_remaining", 14),
                },
            )
            route_message(
                agent="intel_feed",
                message=(
                    "<b>Intel_feed started.</b> Warmup mode active for 14 days. "
                    "Reduced surfacing during this period is expected behavior."
                ),
                priority="informational",
            )

    # Generate active tracks (once per scan)
    active_tracks = get_or_generate_active_tracks(scan_id)

    # Fetch items
    all_items = fetch_all_sources()
    logger.info("Scan %s: fetched %d items", scan_id, len(all_items))

    # Pipeline per item
    scanned_signals = []
    surfaced_count = 0

    for item in all_items:
        source = next(
            (s for s in sources if s["id"] == item.get("source_id")),
            {"id": item.get("source_id", "unknown"), "class": 1},
        )

        # Sanitize
        sanitized = sanitize_item(item)
        if not sanitized["success"]:
            continue

        title = sanitized["sanitized_title"]
        summary = sanitized["sanitized_summary"]

        # Gates
        gates_result = evaluate_all_gates(
            title, summary, item.get("source_id", ""), active_tracks, warmup_active,
        )

        # Determine surfacing
        should_surface = gates_result.get("all_gates_passed", False)
        if should_surface and surfaced_count >= max_surfaces:
            should_surface = False

        # Build signal — surfaced starts False and is only set True after a
        # CONFIRMED Telegram send (RPOSE-FIND7). Archiving is deferred until
        # after the send attempt so the persisted record never claims a delivery
        # that did not actually happen.
        signal = _build_external_signal(
            item, source, sanitized, gates_result, active_tracks,
            warmup_active, surfaced=False,
        )
        scanned_signals.append(signal)
        _total_scored += 1

        # Surface if gates passed — mark surfaced only on confirmed send success.
        if should_surface:
            try:
                msg = _build_telegram_message(signal, source)
                route_result = route_message(
                    agent="intel_feed",
                    message=msg,
                    priority="informational",
                )
                if route_result.get("sent"):
                    signal["surfaced"] = True
                    signal["surfaced_at"] = datetime.now(timezone.utc).isoformat()
                    surfaced_count += 1
                    logger.info("Surfaced: %s (sent=True)", signal["title"][:60])
                else:
                    logger.warning(
                        "Surface not delivered: %s (reason=%s)",
                        signal["title"][:60], route_result.get("reason"),
                    )
            except Exception as exc:
                logger.error("Failed to surface: %s", exc)

        # Archive once, with the now-accurate surfaced state.
        _archive_signal(signal)

    # Cost estimate
    total_cost = _estimate_cost(len(scanned_signals), "heuristic")

    # Update scan state
    _last_scan_time = time.time()
    schedule_config = config.get("schedule", {})
    hours_between = 24 / schedule_config.get("scans_per_day", 3)
    _next_scan_time = _last_scan_time + hours_between * 3600
    _total_scans += 1
    _total_surfaced += surfaced_count
    _daily_cost_estimate += total_cost

    # Log scan metadata
    log_system_event(
        namespace="system-events",
        agent="intel_feed",
        message_preview=f"Scan {scan_id} complete",
        extra={
            "event_type": "scan_complete",
            "scan_id": scan_id,
            "items_fetched": len(all_items),
            "items_scored": len(scanned_signals),
            "items_surfaced": surfaced_count,
            "warmup_active": warmup_active,
            "cost_estimate": round(total_cost, 4),
            "duration_seconds": round(time.time() - scan_start, 2),
        },
    )

    logger.info(
        "=== Scan %s: %d fetched, %d scored, %d surfaced, $%.4f ===",
        scan_id, len(all_items), len(scanned_signals), surfaced_count, total_cost,
    )

    return {
        "scan_id": scan_id,
        "items_fetched": len(all_items),
        "items_scored": len(scanned_signals),
        "items_surfaced": surfaced_count,
        "warmup_active": warmup_active,
        "cost_estimate": round(total_cost, 4),
        "signals": scanned_signals,
    }


def run_test(source_id: str) -> dict | None:
    """Run a test: fetch one item, sanitize, score — no archive write."""
    config = get_intel_feed_config()
    sources = get_sources()
    source = next((s for s in sources if s["id"] == source_id), None)
    if not source:
        return None

    item = test_fetch_source(source_id)
    if not item:
        return None

    active_tracks = get_or_generate_active_tracks("test-scan")

    sanitized = sanitize_item(item)
    if not sanitized["success"]:
        return {
            "source_id": source_id,
            "title": item.get("title", ""),
            "error": "Sanitization failed",
            "sanitization": sanitized,
        }

    title = sanitized["sanitized_title"]
    summary = sanitized["sanitized_summary"]

    gates_result = evaluate_all_gates(title, summary, source_id, active_tracks)

    return {
        "source_id": source_id,
        "original_title": item.get("title", ""),
        "sanitized_title": title,
        "sanitized_summary": summary[:200],
        "url": item.get("url", ""),
        "sanitization": {
            "title_stripped": sanitized["title_stripped"],
            "summary_stripped": sanitized["summary_stripped"],
        },
        "gates": {
            "keyword": gates_result.get("gate_keyword", False),
            "llm_score": gates_result.get("gate_llm_score", 0.0),
            "novelty_score": gates_result.get("gate_novelty_score", 0.0),
            "all_passed": gates_result.get("all_gates_passed", False),
        },
        "active_tracks": active_tracks[:3],
        "note": "Test mode — no write to archive, no Telegram message",
    }


def get_status() -> dict:
    """Return agent health and status."""
    global _last_scan_time, _next_scan_time, _total_scans, _total_scored, _total_surfaced, _daily_cost_estimate

    warmup_info = is_warmup_active()
    config = get_intel_feed_config()

    return {
        "agent": "intel_feed",
        "status": "healthy",
        "version": config.get("version", "1.0"),
        "last_scan": (
            datetime.fromtimestamp(_last_scan_time, tz=timezone.utc).isoformat()
            if _last_scan_time else None
        ),
        "next_scan": (
            datetime.fromtimestamp(_next_scan_time, tz=timezone.utc).isoformat()
            if _next_scan_time else None
        ),
        "total_scans": _total_scans,
        "total_scored": _total_scored,
        "total_surfaced": _total_surfaced,
        "archive_count": len(_archive_records),
        "warmup_active": warmup_info.get("active", False),
        "warmup_days_remaining": round(warmup_info.get("days_remaining", 0), 1),
        "daily_cost_estimate": round(_daily_cost_estimate, 4),
        "max_daily_usd": config.get("cost", {}).get("max_daily_usd", 2.00),
    }


def get_observations(last_days: int = 7, surfaced_only: bool = False) -> list[dict]:
    """Get recent observations from archive."""
    cutoff = time.time() - last_days * 86400
    results = []
    for entry in _archive_records:
        ts_str = entry.get("fetched_at", "")
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
        except (ValueError, AttributeError):
            ts = time.time()
        if ts < cutoff:
            continue
        if surfaced_only and not entry.get("surfaced"):
            continue
        results.append(entry)
    return results


def reset_engine():
    """Reset all engine state (for testing)."""
    global _last_scan_time, _next_scan_time, _total_scans
    global _total_scored, _total_surfaced, _daily_cost_estimate, _first_startup_done, _archive_records
    _last_scan_time = None
    _next_scan_time = None
    _total_scans = 0
    _total_scored = 0
    _total_surfaced = 0
    _daily_cost_estimate = 0.0
    _first_startup_done = False
    _archive_records = []
    reset_fetch_state()
    reset_tracks_cache()
    reset_warmup()
    _archive_cache.clear()
    _archive_records.clear()
    reload_all()
