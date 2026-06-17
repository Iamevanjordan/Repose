"""Main scan pipeline for Intel_feed Lite.

Orchestrates a full scan cycle:
  fetch → sanitize → keyword_gate → [skip if fail] →
  generate_active_tracks (once per scan, cached) →
  llm_score(haiku) → [escalate to sonnet if confidence < 0.6] →
  novelty_score(voyage-3) →
  all_gates_eval →
  write_to_chronogram(intel_feed-archive) →
  [if all_gates_passed] telegram_router.route_message(priority="informational")
"""

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from repose.agents.intel_feed.config import get_intel_feed_config, get_keywords
from repose.agents.intel_feed.ingestion import fetch_all_sources, test_fetch_source
from repose.agents.intel_feed.sanitization import sanitize_item
from repose.agents.intel_feed.tracks import get_or_generate_active_tracks
from repose.agents.intel_feed.scoring import (
    score_item_llm,
    score_novelty,
    is_warmup_active,
    get_warmup_max_surfaces,
    reset_warmup,
)
from repose.agents.intel_feed.gates import (
    gate_keyword,
    gate_llm_relevance,
    gate_novelty,
    evaluate_all_gates,
)
from repose.agents.intel_feed.archiver import (
    build_external_signal,
    archive_signal,
    get_archive_records,
    count_archive_records,
)
from repose.utils.chronogram import log_system_event

logger = logging.getLogger(__name__)

# Scan state
_last_scan_time: Optional[str] = None
_scan_count: int = 0

# Source class emoji mapping (Section 9)
SOURCE_EMOJI = {1: "\U0001f9e0", 2: "\U0001f4a1", 3: "\U0001f3af"}


def _format_telegram_message(
    source_id: str,
    source_class: int,
    title: str,
    llm_score: float,
    novelty_score: float,
    url: str,
) -> str:
    """Format a Telegram message per Section 9."""
    emoji = SOURCE_EMOJI.get(source_class, "\U0001f4e1")
    return (
        f"INTEL_FEED \u00b7 {emoji} {source_id}\n"
        f"{title}\n"
        f"Relevance: {llm_score:.2f} \u00b7 Novelty: {novelty_score:.2f}\n"
        f"{url}"
    )


def _surface_to_telegram(record: dict) -> dict:
    """Send a surfaced item to Telegram via shared router.

    Telegram delivery may fail during build — do not block on it.
    Returns the routing result dict.
    """
    try:
        from repose.utils.telegram_router import route_message

        config = get_intel_feed_config()
        priority = config.get("telegram", {}).get("priority_class", "informational")

        message = _format_telegram_message(
            source_id=record["source_id"],
            source_class=record["source_class"],
            title=record["title"],
            llm_score=record["gate_llm_score"],
            novelty_score=record["gate_novelty_score"],
            url=record["url"],
        )

        result = route_message(
            agent="intel_feed",
            message=message,
            priority=priority,
        )

        if result["sent"]:
            logger.info("Surfaced to Telegram: %s", record["signal_id"])
        else:
            logger.warning(
                "Telegram surface failed for %s: %s",
                record["signal_id"], result.get("reason", "unknown"),
            )

        return result
    except Exception as exc:
        logger.warning("Telegram routing unavailable: %s", exc)
        return {"sent": False, "reason": str(exc)}


def _cold_start_telegram_message() -> dict | None:
    """Send cold-start warmup notification to Telegram (once on first startup)."""
    from pathlib import Path
    import json

    notify_file = Path(__file__).resolve().parent.parent.parent.parent / ".intel_feed_startup_notified.json"

    if notify_file.exists():
        return None  # Already notified

    warmup = is_warmup_active()
    if not warmup["active"]:
        return None

    # Send startup message
    message = (
        "Intel_feed started. Warmup mode active for 14 days. "
        "Reduced surfacing during this period is expected behavior."
    )

    try:
        from repose.utils.telegram_router import route_message

        result = route_message(
            agent="intel_feed",
            message=message,
            priority="informational",
        )

        # Log the cold_start_initiated event
        log_system_event(
            namespace="system-events",
            agent="intel_feed",
            message_preview=message[:100],
            extra={
                "event_type": "cold_start_initiated",
                "warmup_days": warmup["warmup_days"],
                "warmup_start": warmup["warmup_start_iso"],
            },
        )

        # Mark as notified
        notify_file.write_text(json.dumps({"notified_at": datetime.now(timezone.utc).isoformat()}))

        return result
    except Exception as exc:
        logger.warning("Cold-start notification failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Single item pipeline (for test command)
# ---------------------------------------------------------------------------

def process_single_item(
    item: dict,
    active_tracks: list[str],
    keywords: list[str],
    warmup_mode: bool = False,
    do_archive: bool = False,
    do_surface: bool = False,
) -> dict:
    """Process a single item through the full scoring pipeline.

    Args:
        item: Signal item dict (title, summary, url, source_id, source_class).
        active_tracks: Active tracks for this scan.
        keywords: Keywords for Gate 1.
        warmup_mode: Whether cold-start warmup is active.
        do_archive: If True, write to archive.
        do_surface: If True, surface to Telegram if gates pass.

    Returns:
        dict with full processing results.
    """
    source_id = item.get("source_id", "unknown")
    source_class = item.get("source_class", 1)

    # Step 1: Sanitize
    sanitized = sanitize_item(item)
    if not sanitized["success"]:
        return {
            "signal_id": None,
            "source_id": source_id,
            "error": sanitized.get("error", "sanitization_failure"),
            "stage": "sanitization",
            "skipped": True,
        }

    title = sanitized["sanitized_title"]
    summary = sanitized["sanitized_summary"]

    # Step 2: Keyword gate
    kw_result = gate_keyword(title, summary, keywords)
    if not kw_result["passed"]:
        return {
            "signal_id": None,
            "source_id": source_id,
            "error": f"keyword_gate_failed ({kw_result['matches']}/{kw_result['min_required']})",
            "stage": "keyword_gate",
            "skipped": True,
            "keyword_result": kw_result,
        }

    # Step 3: LLM scoring
    llm_result = score_item_llm(title, summary, source_id, active_tracks)
    llm_score_val = llm_result.get("score", 0.0)

    # Step 4: LLM relevance gate
    llm_gate = gate_llm_relevance(llm_score_val, llm_result.get("confidence", 0.0), warmup_mode)

    # Step 5: Novelty scoring
    novelty_result = score_novelty(title, summary, source_id)
    novelty_val = novelty_result.get("score", 1.0)

    # Step 6: Novelty gate
    nov_gate = gate_novelty(novelty_val)

    # Step 7: All gates evaluation
    gates = evaluate_all_gates(kw_result=kw_result, llm_gate=llm_gate, nov_gate=nov_gate)

    # Step 8: Build external_signal record
    record = build_external_signal(
        source_id=source_id,
        source_class=source_class,
        title=title,
        summary=summary,
        url=item.get("url", ""),
        keyword_result=kw_result,
        llm_score=llm_score_val,
        novelty_score=novelty_val,
        all_gates_passed=gates["all_passed"],
        surfaced=False,
        active_tracks_snapshot=active_tracks,
        warmup_mode=warmup_mode,
        fetched_at=item.get("published"),
    )

    # Step 9: Archive
    if do_archive:
        archive_signal(record)

    # Step 10: Surface to Telegram
    if do_surface and gates["all_passed"]:
        surface_result = _surface_to_telegram(record)
        if surface_result.get("sent"):
            record["surfaced"] = True
            record["surfaced_at"] = datetime.now(timezone.utc).isoformat()
            # Re-archive with surfaced flag
            if do_archive:
                archive_signal(record)

    return {
        "signal_id": record["signal_id"],
        "source_id": source_id,
        "source_class": source_class,
        "title": title,
        "gates": gates,
        "llm_score": llm_score_val,
        "novelty_score": novelty_val,
        "record": record,
        "skipped": False,
        "surfaced": record["surfaced"],
    }


# ---------------------------------------------------------------------------
# Full scan pipeline
# ---------------------------------------------------------------------------

def run_scan(
    specific_source: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Run a full scan cycle across all sources.

    Args:
        specific_source: If set, only scan this source ID.
        dry_run: If True, process items but don't archive or surface.

    Returns:
        dict with scan metadata and results.
    """
    global _last_scan_time, _scan_count

    scan_id = str(uuid.uuid4())
    scan_start = datetime.now(timezone.utc)
    config = get_intel_feed_config()

    # Check cold-start warmup
    warmup = is_warmup_active()
    warmup_mode = warmup["active"]
    warmup_max = get_warmup_max_surfaces() if warmup_mode else 999

    # Send cold-start notification on first scan
    if warmup_mode and _scan_count == 0:
        _cold_start_telegram_message()

    logger.info(
        "=== Scan %s starting === (warmup=%s, dry_run=%s)",
        scan_id, warmup_mode, dry_run,
    )

    # Generate active tracks (once per scan, cached)
    active_tracks = get_or_generate_active_tracks(scan_id)
    keywords = get_keywords()

    # Fetch items from all sources
    if specific_source:
        item = test_fetch_source(specific_source)
        items = [item] if item else []
    else:
        items = fetch_all_sources()

    logger.info("Scan %s: fetched %d items total", scan_id, len(items))

    # Process each item
    results = []
    surfaced_count = 0
    skipped_count = 0

    for item in items:
        result = process_single_item(
            item=item,
            active_tracks=active_tracks,
            keywords=keywords,
            warmup_mode=warmup_mode,
            do_archive=not dry_run,
            do_surface=(not dry_run and surfaced_count < warmup_max),
        )

        if result["skipped"]:
            skipped_count += 1
        else:
            results.append(result)
            if result.get("surfaced"):
                surfaced_count += 1

        # Enforce warmup max surfaces cap
        if warmup_mode and surfaced_count >= warmup_max:
            logger.info(
                "Warmup surface cap reached (%d), remaining items will not surface",
                warmup_max,
            )

    # Update scan state
    _last_scan_time = scan_start.isoformat()
    _scan_count += 1

    # Log scan complete
    log_system_event(
        namespace="system-events",
        agent="intel_feed",
        message_preview=f"Scan {scan_id} complete",
        extra={
            "event_type": "scan_complete",
            "scan_id": scan_id,
            "items_fetched": len(items),
            "items_processed": len(results),
            "items_skipped": skipped_count,
            "items_surfaced": surfaced_count,
            "warmup_mode": warmup_mode,
            "dry_run": dry_run,
        },
    )

    scan_end = datetime.now(timezone.utc)
    duration = (scan_end - scan_start).total_seconds()

    return {
        "scan_id": scan_id,
        "scan_start": scan_start.isoformat(),
        "scan_end": scan_end.isoformat(),
        "duration_seconds": round(duration, 2),
        "items_fetched": len(items),
        "items_processed": len(results),
        "items_skipped": skipped_count,
        "items_surfaced": surfaced_count,
        "warmup_mode": warmup_mode,
        "warmup_days_remaining": warmup.get("days_remaining", 0),
        "results": results,
    }


# ---------------------------------------------------------------------------
# Status / health
# ---------------------------------------------------------------------------

def get_status() -> dict:
    """Return agent health and status for `repose intel_feed status`."""
    config = get_intel_feed_config()
    schedule = config.get("schedule", {})
    warmup = is_warmup_active()

    archive_count = count_archive_records()

    return {
        "agent": "intel_feed",
        "version": config.get("version", "1.0"),
        "healthy": True,
        "last_scan": _last_scan_time,
        "next_scan": "scheduled",  # Computed from schedule in production
        "scan_count": _scan_count,
        "scan_times_utc": schedule.get("scan_times_utc", []),
        "scans_per_day": schedule.get("scans_per_day", 3),
        "timezone": schedule.get("timezone", "America/New_York"),
        "warmup": {
            "active": warmup["active"],
            "days_remaining": warmup["days_remaining"],
            "warmup_days": warmup["warmup_days"],
            "started_at": warmup["warmup_start_iso"],
        },
        "archive_record_count": archive_count,
        "cost": {
            "max_daily_usd": config.get("cost", {}).get("max_daily_usd", 2.00),
        },
    }


def reset_scan_state():
    """Reset scan state (for testing)."""
    global _last_scan_time, _scan_count
    _last_scan_time = None
    _scan_count = 0
    reset_warmup()
    from pathlib import Path
    startup_file = Path(__file__).resolve().parent.parent.parent.parent / ".intel_feed_startup_notified.json"
    startup_file.unlink(missing_ok=True)
