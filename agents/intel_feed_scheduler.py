"""
Intel_feed scheduler daemon - Repose OS.

Long-lived scheduler that triggers Intel_feed intelligence scans on the cadence
defined in config/intel_feed.yaml (schedule.scan_times_utc). Mirrors the Morning_brief
cron pattern (fixed daily UTC times) but runs as an always-on systemd
service so its status is directly observable.

At each scheduled slot it calls repose.agents.intel_feed.engine.run_scan() in
process. run_scan() owns its warmup gating, daily cost cap, sanitization,
and Telegram routing - this scheduler only decides *when*.
"""
from __future__ import annotations

import logging
import signal
import sys
import threading
from datetime import datetime, timedelta, timezone

from repose.agents.intel_feed.config import get_intel_feed_config
from repose.agents.intel_feed.engine import run_scan
from repose.utils.daemon import DaemonGuard

logger = logging.getLogger("intel_feed.scheduler")

DEFAULT_SCAN_TIMES_UTC = ["06:00", "13:00", "20:00"]


def _scan_times() -> list[str]:
    try:
        cfg = get_intel_feed_config()
        times = cfg.get("schedule", {}).get("scan_times_utc") or DEFAULT_SCAN_TIMES_UTC
    except Exception:
        times = DEFAULT_SCAN_TIMES_UTC
    return sorted(times)


def _seconds_until_next(now: datetime, times: list[str]) -> tuple[float, str]:
    """Return (seconds until next HH:MM UTC slot, slot label)."""
    candidates = []
    for t in times:
        hh, mm = (int(x) for x in t.split(":"))
        slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if slot <= now:
            slot = slot + timedelta(days=1)
        candidates.append((slot, t))
    slot, label = min(candidates, key=lambda c: c[0])
    return (slot - now).total_seconds(), label


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # Single-instance guard: refuse to start a second scheduler if a live one
    # is already running (orphaned across a docker-exec systemd restart).
    if not DaemonGuard("intel_feed_scheduler").acquire():
        return 1
    stop = threading.Event()

    def _handle_signal(signum, _frame):
        logger.info("Intel_feed scheduler received signal %s - shutting down", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    times = _scan_times()
    logger.info("Intel_feed scheduler starting - scan_times_utc=%s", times)

    while not stop.is_set():
        wait_s, label = _seconds_until_next(datetime.now(timezone.utc), times)
        logger.info("next Intel_feed scan at %s UTC (in %.0fs)", label, wait_s)
        if stop.wait(wait_s):
            break
        try:
            logger.info("triggering Intel_feed scan (slot %s UTC)", label)
            result = run_scan()
            logger.info(
                "Intel_feed scan complete: id=%s fetched=%s scored=%s surfaced=%s cost=%s",
                result.get("scan_id"),
                result.get("items_fetched"),
                result.get("items_scored"),
                result.get("items_surfaced"),
                result.get("cost_estimate"),
            )
        except Exception as exc:
            logger.warning("Intel_feed scan error (continuing): %s", exc)
        # guard against double-trigger inside the same minute
        stop.wait(61)

    logger.info("Intel_feed scheduler stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
