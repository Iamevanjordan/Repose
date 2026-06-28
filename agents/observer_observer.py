"""
Observer observer daemon - Repose OS.

Cold-start observer daemon entry point. It runs Observer's monitoring checks
on a fixed interval. The daemon only reads and records; it never delivers to
the operator itself.

Cold-start behavior (warmup window active through ~2026-07-02):
  - Only check_* functions are called; each ORCA *read* is wrapped in
    _assert_read_only("read"). Observation *recording* is not gated by this:
    write_observation / log_system_event proceed to durable memory normally
    throughout cold-start.
  - This daemon never calls surface_observation() -> it delivers nothing to
    Telegram itself. Operator-facing surfacing is withheld per-agent until each
    agent clears its warmup grace window (observer_core.check_quality_drift /
    config/observer.yaml: per_agent_cold_start_grace).
  - No baseline is persisted, reset, or initialized (baselines are computed
    live and in-memory by observer_core).
  - write_mode_activation_date (config/observer.yaml) governs only the ORCA
    mutating-operation capability lock; it never gates observation recording.

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
    # polled. Before write_mode_activation_date (config/observer.yaml) the ORCA
    # mutating-operation capability stays locked; on/after that date it unlocks
    # those operations. It does not itself perform writes, and it never gates
    # observation recording — durable observation writes proceed throughout.
    if w.check_and_apply_write_mode_transition():
        logger.warning("Observer startup: ORCA write mode ACTIVE (mutating-op lock lifted)")
    else:
        logger.info("Observer startup: ORCA mutating ops locked (cold-start); recording active")

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
        "Observer observer starting - cold-start warmup mode "
        "(durable writes proceed; daemon does not surface), interval=%ss",
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
