"""Daemon lifecycle helpers for Repose OS long-lived services.

The Repose daemons (intel_feed_scheduler, observer_observer, event_monitor) run *inside* the
workspace container, launched by systemd via ``docker exec``. Because the
in-container process is not part of systemd's cgroup, a unit restart can leave
the previous process running and start a second one on top of it — two
schedulers firing scans, two observers, two webhook servers.

DaemonGuard prevents that with a PID-file singleton:

  * On start it reads ``/tmp/repose_<name>.pid``. If that file names a *live*
    process it logs an ERROR and refuses to start — it never kills the
    incumbent (fail safe: do not SIGKILL a sibling that may be mid-write).
  * If the PID file is missing, stale (no such process) or unreadable, it
    reclaims it and writes this process's PID.
  * It registers an ``atexit`` cleanup that removes the PID file (only if it
    still belongs to this process) and terminates any child processes that were
    registered via ``register_child``. The daemons' existing SIGTERM/SIGINT
    handlers already unwind to a clean interpreter exit, which triggers atexit;
    SIGKILL leaves a stale PID file that the next start reclaims safely.
"""
from __future__ import annotations

import atexit
import logging
import os
from pathlib import Path

logger = logging.getLogger("repose.daemon")

PID_DIR = Path("/tmp")


def _pid_alive(pid: int) -> bool:
    """True if a process with ``pid`` currently exists."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    return True


class DaemonGuard:
    """Single-instance PID guard + cleanup hook for a named daemon."""

    def __init__(self, name: str):
        self.name = name
        self.pid_file = PID_DIR / f"repose_{name}.pid"
        self._children: list = []
        self._owned = False

    def acquire(self) -> bool:
        """Claim singleton ownership.

        Returns True if this process may run, False if a live duplicate already
        holds the PID file (caller should exit non-zero without doing work).
        """
        if self.pid_file.exists():
            try:
                existing = int(self.pid_file.read_text().strip())
            except (ValueError, OSError):
                existing = -1
            if existing > 0 and existing != os.getpid() and _pid_alive(existing):
                logger.error(
                    "%s already running as PID %d (this PID %d); refusing to "
                    "start a duplicate. Not touching the existing process.",
                    self.name, existing, os.getpid(),
                )
                return False
            # Stale or unreadable PID file — reclaim it.
            try:
                self.pid_file.unlink()
            except OSError:
                pass
        try:
            self.pid_file.write_text(str(os.getpid()))
        except OSError as exc:
            logger.error(
                "%s could not write PID file %s: %s", self.name, self.pid_file, exc
            )
            return False
        self._owned = True
        atexit.register(self.cleanup)
        logger.info("%s acquired singleton lock (PID %d, %s)",
                    self.name, os.getpid(), self.pid_file)
        return True

    def register_child(self, proc) -> None:
        """Register a child process (anything with terminate()/kill()/pid) to be
        cleaned up when this daemon exits."""
        self._children.append(proc)

    def cleanup(self) -> None:
        """Terminate registered children and remove our PID file (idempotent)."""
        for proc in self._children:
            try:
                if getattr(proc, "poll", lambda: None)() is None:
                    proc.terminate()
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass
        self._children = []
        # Only remove the PID file if it still belongs to us — never delete a
        # file that a replacement process has since written.
        if not self._owned:
            return
        try:
            if self.pid_file.exists():
                current = self.pid_file.read_text().strip()
                if current == str(os.getpid()):
                    self.pid_file.unlink()
        except OSError:
            pass
        self._owned = False
