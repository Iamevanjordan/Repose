"""
Observer observer daemon - Repose OS.

Cold-start, READ-ONLY observer entry point. It runs Observer's existing
read-only monitoring checks on a fixed interval and does nothing else.

Read-only guarantees (cold-start window active through ~2026-07-02):
  - Only check_* functions are called; each calls _assert_read_only("read").
  - surface_observation() is NEVER called -> nothing reaches Telegram.
  - No baseline is persisted, reset, or initialized (baselines are computed
    live and in-memory by observer_core).
  - observer_core.write_observation / log_system_event are in-memory only;
    no durable namespace or state-file writes occur.

If _assert_read_only ever trips (AssertionError), the daemon exits non-zero
loudly rather than silently continuing.

Interval defaults to the tightest cadence in observer.yaml (substrate 15m);
override via the optional `observer.interval_seconds` config key.
"""
from __future__ import annotations

import logging
import signal
import sys
import threading

from repose.agents import observer_core as w
from repose.utils.daemon import DaemonGuard

logger = logging.getLogger("observer.observer")

DEFAULT_INTERVAL_SECONDS = 900  # 15 minutes (matches substrate_health cron */15)


def run_once() -> dict:
    """Run all read-only checks once and return a summary. No surfacing."""
    eh = w.check_execution_health()
    sh = w.check_substrate_health()
    qd = w.check_quality_drift()
    ae = w.check_ack_expiry()
    summary = {
        "execution_health": len(eh),
        "substrate_health": len(sh),
        "quality_drift": len(qd),
        "ack_expiry": len(ae),
    }
    logger.info("observer cycle (read-only, surfacing disabled): %s", summary)
    return summary


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # Single-instance guard: refuse to start a second observer if a live one is
    # already running (orphaned across a docker-exec systemd restart). Read-only
    # behavior is unchanged — this only manages a PID file in /tmp.
    if not DaemonGuard("observer_observer").acquire():
        return 1

    # Write-mode transition (mechanism only). Evaluated once at startup, never
    # polled. Before write_mode_activation_date (config/observer.yaml) this is a
    # no-op and Observer stays read-only; on/after that date it unlocks write
    # operations. It does not itself perform writes.
    if w.check_and_apply_write_mode_transition():
        logger.warning("Observer startup: write mode ACTIVE (cold-start lock lifted)")
    else:
        logger.info("Observer startup: read-only (cold-start lock in effect)")

    cfg = w.get_config()
    interval = int(
        cfg.get("observer", {}).get("interval_seconds", DEFAULT_INTERVAL_SECONDS)
    )

    stop = threading.Event()

    def _handle_signal(signum, _frame):
        logger.info("Observer observer received signal %s - shutting down", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info(
        "Observer observer starting - READ-ONLY cold-start mode, "
        "interval=%ss, surfacing=DISABLED",
        interval,
    )
    while not stop.is_set():
        try:
            run_once()
        except AssertionError as exc:
            logger.critical("READ-ONLY ASSERTION TRIPPED: %s", exc)
            return 1
        except Exception as exc:  # observer must stay resilient
            logger.warning("observer cycle error (continuing): %s", exc)
        stop.wait(interval)

    logger.info("Observer observer stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
