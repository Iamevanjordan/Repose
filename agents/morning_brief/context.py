"""
Morning_brief context builder.
Assembles all inputs for brief composition from Chronogram namespaces.
Reads namespace names and query parameters from /config/morning_brief.yaml.
Graceful fallback for any namespace that is empty, missing, or errors.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

_BUSINESS_STATE_RECENCY_DAYS = 7


@dataclass
class Morning_briefContext:
    """All inputs to Morning_brief brief composition. Empty = namespace had no data."""
    business_focus: list[dict] = field(default_factory=list)
    system_events: list[dict] = field(default_factory=list)
    open_decisions: list[dict] = field(default_factory=list)
    intel_feed_items: list[dict] = field(default_factory=list)       # empty until M3
    event_monitor_alerts: list[dict] = field(default_factory=list)      # empty until M4
    design_activity: list[dict] = field(default_factory=list)   # empty until M5
    options_summary: Optional[dict] = None                      # None until OA build
    build_errors: list[str] = field(default_factory=list)
    # build_errors: non-fatal retrieval failures, written to system-events after build


def _is_recent(record: dict, days: int = _BUSINESS_STATE_RECENCY_DAYS) -> bool:
    """Return True if record was created within the last N days.

    Chronogram returns createdAt at the top level of each record dict.
    Custom metadata.written_at is not preserved in recall responses.
    """
    ts_str = record.get("createdAt", "")
    if not ts_str:
        return False
    try:
        from dateutil import parser as dtparser
        record_time = dtparser.parse(ts_str)
        if record_time.tzinfo is None:
            record_time = record_time.replace(tzinfo=timezone.utc)
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        return record_time > cutoff
    except Exception:
        return False


def build_context(client, config: dict) -> "Morning_briefContext":
    """
    Build Morning_brief context from configured namespaces.
    Each namespace call is isolated — failure in one never kills the brief.
    build_errors collects non-fatal failures for post-build logging.
    Returns Morning_briefContext with whatever data was retrievable.
    client: ChronogramClient instance (synchronous)
    """
    ctx = Morning_briefContext()
    sections = config.get("brief", {}).get("sections", [])

    for section in sections:
        section_id = section["id"]
        source = section.get("source")
        if not source:
            continue
        try:
            data = _fetch_section(client, section)
            _populate_context(ctx, section_id, data)
        except Exception as e:
            error_msg = (
                f"Context fetch failed for section '{section_id}' (source: {source}): "
                f"{type(e).__name__}: {e}"
            )
            ctx.build_errors.append(error_msg)
            logger.warning(error_msg)

    data_count = sum([
        len(ctx.business_focus), len(ctx.system_events), len(ctx.open_decisions),
        len(ctx.intel_feed_items), len(ctx.event_monitor_alerts), len(ctx.design_activity),
        1 if ctx.options_summary else 0,
    ])
    logger.info(
        "Morning_brief context built: %d items across sections, %d errors",
        data_count, len(ctx.build_errors),
    )
    return ctx


def _fetch_section(client, section: dict) -> list:
    """Dispatch namespace fetch based on section config using recall()."""
    source = section["source"]
    limit = section.get("top_n") or section.get("limit", 10)
    query = section.get("query", "")
    status = section.get("status")

    if status:
        # decision-queue: query by status using recall with status as query term
        query = f"status:{status} {query}".strip()

    if not query:
        query = f"recent entries in {source}"

    try:
        if source == "business-state":
            # Request extra candidates so the recency filter has enough to work with
            raw = client.recall(namespace=source, query=query, limit=max(limit * 3, 15))
            fresh = [r for r in raw if _is_recent(r)]
            logger.info(
                "business-state recall: %d raw → %d after recency filter (7d)",
                len(raw), len(fresh),
            )
            return fresh[:limit]
        return client.recall(namespace=source, query=query, limit=limit)
    except Exception:
        # Namespace may not exist yet (M3+ namespaces) — return empty list
        return []


def _populate_context(ctx: Morning_briefContext, section_id: str, data: list) -> None:
    """Write fetched data into the correct Morning_briefContext field."""
    mapping = {
        "focus": "business_focus",
        "system_health": "system_events",
        "decisions": "open_decisions",
        "overnight_intel": "intel_feed_items",
        "alerts": "event_monitor_alerts",
        "design_activity": "design_activity",
        "options_status": None,  # handled separately
    }
    if section_id == "options_status":
        ctx.options_summary = data[0] if data else None
        return
    field_name = mapping.get(section_id)
    if field_name:
        setattr(ctx, field_name, data or [])
