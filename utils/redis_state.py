"""Shared Redis connection + coordination state for Repose OS (RPOSE-007).

Redis is the single source of truth for cross-worker coordination state.
Production coordination state must NOT live in process-local module globals —
multiple workers cannot share truth that way. Every accessor here is backed by
Redis.

There is NO process-local fallback: if Redis is unreachable, every accessor
raises RedisStateError immediately (fail closed) so workers can never silently
diverge on stale local copies.

Connection parameters come from Bitwarden (keys: repose-redis-host,
repose-redis-port) — Bitwarden is the only secrets/config-of-record layer
(RPOSE-008). No environment variables, no .env files.
"""

import logging

logger = logging.getLogger(__name__)

# Cache one verified connection per Redis db.
_connections: dict = {}


class RedisStateError(Exception):
    """Raised when Redis-backed coordination state is unavailable.

    Fatal by design — callers must fail closed and never fall back to
    process-local state (RPOSE-007).
    """


def _redis_host_port() -> tuple:
    """Read Redis host/port from Bitwarden. Raises if Bitwarden is unreachable."""
    from repose.utils.bitwarden import get_secret
    host = get_secret("repose-redis-host")
    port = int(get_secret("repose-redis-port"))
    return host, port


def get_redis(db: int = 0):
    """Return a cached, ping-verified Redis client for the given db.

    Raises RedisStateError immediately if Redis cannot be reached. No fallback.
    """
    if db in _connections:
        return _connections[db]
    try:
        import redis
        host, port = _redis_host_port()
        conn = redis.Redis(
            host=host,
            port=port,
            db=db,
            socket_connect_timeout=2,
            decode_responses=True,
        )
        conn.ping()
    except Exception as e:  # noqa: BLE001 — fatal, fail closed
        raise RedisStateError(
            f"Redis unreachable for db={db}: {e}. Coordination state requires "
            f"Redis; refusing process-local fallback (RPOSE-007)."
        ) from e
    _connections[db] = conn
    logger.info("Redis coordination connection established (db=%d)", db)
    return conn


def reset_connections() -> None:
    """Drop cached connections (used by tests)."""
    _connections.clear()
