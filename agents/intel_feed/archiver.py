"""Chronogram archiver for Intel_feed Lite.

Writes external_signal records to intel_feed-archive namespace.
Manages the archive for novelty comparison.
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from repose.agents.intel_feed.config import get_intel_feed_config
from repose.utils.chronogram import log_system_event

logger = logging.getLogger(__name__)

# Local archive file (fallback when Redis/Chronogram is unavailable)
_ARCHIVE_FILE = Path(__file__).resolve().parent.parent.parent.parent / ".intel_feed_archive.jsonl"


def _write_to_local_archive(record: dict) -> None:
    """Append a record to the local archive JSONL file."""
    try:
        with open(_ARCHIVE_FILE, "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError as exc:
        logger.error("Failed to write to local archive: %s", exc)


def _read_local_archive(limit: int = 100) -> list[dict]:
    """Read recent records from the local archive JSONL file."""
    if not _ARCHIVE_FILE.exists():
        return []

    records = []
    try:
        with open(_ARCHIVE_FILE) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except OSError as exc:
        logger.error("Failed to read local archive: %s", exc)
        return []

    # Return most recent first, limited
    return list(reversed(records))[-limit:]


def build_external_signal(
    source_id: str,
    source_class: int,
    title: str,
    summary: str,
    url: str,
    keyword_result: dict,
    llm_score: float,
    novelty_score: float,
    all_gates_passed: bool,
    surfaced: bool,
    active_tracks_snapshot: list[str],
    warmup_mode: bool = False,
    fetched_at: Optional[str] = None,
) -> dict:
    """Build an external_signal record matching the canonical schema (Section 6).

    Args:
        source_id: Source identifier (e.g., 'arxiv_cs_ai').
        source_class: Source class (1, 2, or 3).
        title: Sanitized title.
        summary: Sanitized summary (max 500 chars).
        url: Article URL.
        keyword_result: Gate 1 results dict.
        llm_score: Gate 2 LLM score.
        novelty_score: Gate 3 novelty score.
        all_gates_passed: Whether all three gates passed.
        surfaced: Whether the item was sent to Telegram.
        active_tracks_snapshot: Active tracks at time of scan.
        warmup_mode: Whether cold-start warmup was active.
        fetched_at: ISO-8601 fetch timestamp. Default: now.

    Returns:
        dict matching external_signal schema v1.0.
    """
    now = datetime.now(timezone.utc)
    fetched_at_iso = fetched_at or now.isoformat()

    return {
        "signal_id": str(uuid.uuid4()),
        "schema_version": "1.0",
        "source_id": source_id,
        "source_class": source_class,
        "fetched_at": fetched_at_iso,
        "title": title,
        "summary": summary[:500],
        "url": url,
        "gate_keyword": keyword_result.get("passed", False),
        "gate_llm_score": llm_score,
        "gate_novelty_score": novelty_score,
        "all_gates_passed": all_gates_passed,
        "surfaced": surfaced,
        "surfaced_at": now.isoformat() if surfaced else None,
        "active_tracks_snapshot": active_tracks_snapshot,
        "instrument_snapshot_ref": None,
        "warmup_mode": warmup_mode,
    }


def archive_signal(record: dict) -> dict:
    """Write an external_signal record to the shared Chronogram.

    Primary path: repose.utils.chronogram.store_artifact() into the
    intel_feed-archive namespace — a durable, cross-agent write (Chronogram
    host + API key resolved via Bitwarden in that module; nothing hardcoded
    here). Local JSONL is a FALLBACK only, written when Chronogram is
    unreachable so novelty/debugging still has a local record.

    Args:
        record: external_signal dict from build_external_signal().

    Returns:
        The archived record.
    """
    config = get_intel_feed_config()
    chronogram_cfg = config.get("chronogram", {})
    namespace = chronogram_cfg.get("archive_namespace", "intel_feed-archive")

    # Log to system events
    log_system_event(
        namespace="system-events",
        agent="intel_feed",
        message_preview=record["title"][:100],
        extra={
            "event_type": "signal_archived",
            "signal_id": record["signal_id"],
            "source_id": record["source_id"],
            "surfaced": record["surfaced"],
            "all_gates_passed": record["all_gates_passed"],
        },
    )

    # Primary durable persistence: shared Chronogram.
    try:
        from repose.utils.chronogram import store_artifact
        store_artifact(
            namespace=namespace,
            content=json.dumps(record, default=str),
            metadata={
                "source": "intel_feed",
                "type_hint": "episodic",
                "source_id": record["signal_id"],
                "intel_source": record["source_id"],
                "surfaced": record["surfaced"],
                "all_gates_passed": record["all_gates_passed"],
            },
        )
    except Exception as exc:
        # Chronogram unreachable — fall back to local JSONL only.
        logger.warning(
            "Chronogram archive write failed for signal %s; local JSONL fallback: %s",
            record["signal_id"], exc,
        )
        _write_to_local_archive(record)

    # Always update the in-memory archive cache for novelty scoring (in-process).
    try:
        from repose.agents.intel_feed.scoring import _add_to_archive_cache
        _add_to_archive_cache(record)
    except ImportError:
        pass

    logger.info(
        "Archived signal %s from %s (surfaced=%s)",
        record["signal_id"], record["source_id"], record["surfaced"],
    )
    return record


def get_archive_records(
    surfaced_only: bool = False,
    limit: int = 100,
) -> list[dict]:
    """Retrieve records from intel_feed-archive.

    Args:
        surfaced_only: If True, return only surfaced items.
        limit: Maximum records to return.

    Returns:
        List of external_signal records, most recent first.
    """
    records = _read_local_archive(limit * 2)  # Read extra for filtering

    if surfaced_only:
        records = [r for r in records if r.get("surfaced")]

    return list(reversed(records))[:limit]


def count_archive_records() -> int:
    """Count total records in the archive."""
    if not _ARCHIVE_FILE.exists():
        return 0
    try:
        return sum(1 for _ in open(_ARCHIVE_FILE))
    except OSError:
        return 0


def clear_archive():
    """Clear the local archive (for testing)."""
    _ARCHIVE_FILE.unlink(missing_ok=True)
