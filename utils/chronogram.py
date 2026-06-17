"""Chronogram — Orca memory layer for Repose OS (real HTTP client).

This module is the cross-agent memory path. Intel_feed, Event_monitor and Observer all import
from here so that everything they record lands in the shared Chronogram
memory-api instead of a per-process buffer.

Transport: HTTP against the Chronogram memory-api (``/v1/memories/*``).
  - Connection target and the Bitwarden secret id for the API key are read from
    ``repose/config/repose_config.yaml`` under ``chronogram.http``. Nothing is
    hardcoded here and there is NO environment-variable fallback.
  - The API key is resolved through Bitwarden Secrets Manager only
    (RPOSE-008). If Bitwarden is unreachable or the key is missing, the call
    raises ChronogramError — it never silently degrades.

There is intentionally no in-memory fallback store: a failed Chronogram call
raises so the caller fails closed rather than writing to a buffer that no other
agent can read (the original cross-agent memory bug).
"""

import json
import time
import uuid
import logging
import threading

import requests

from repose.config import repose_config
from repose.utils.bitwarden import get_secret

logger = logging.getLogger(__name__)

# HTTP errors are noisy at INFO because httpx/requests log request URLs that can
# embed tokens elsewhere in the stack; keep Chronogram's own logging at DEBUG.
logging.getLogger("urllib3").setLevel(logging.WARNING)


class ChronogramError(Exception):
    """Raised when Chronogram operations fail. Never caught to fall back to an
    in-memory store — callers must fail closed."""


_lock = threading.Lock()
_session: requests.Session | None = None
_http_cfg: dict | None = None
_api_key: str | None = None


def _config() -> dict:
    """Return the validated ``chronogram.http`` config block.

    Raises ChronogramError if the block (or a required key) is missing — there
    is no hardcoded default endpoint.
    """
    global _http_cfg
    if _http_cfg is not None:
        return _http_cfg
    chrono = repose_config.get("chronogram", {}) or {}
    http = chrono.get("http")
    if not http:
        raise ChronogramError(
            "chronogram.http missing from repose_config.yaml; refusing to "
            "guess a Chronogram endpoint."
        )
    for required in ("base_url", "api_key_secret_id"):
        if not http.get(required):
            raise ChronogramError(
                f"chronogram.http.{required} missing from repose_config.yaml."
            )
    _http_cfg = http
    return _http_cfg


def _scope() -> str:
    """Default memory scope for cross-agent writes."""
    return _config().get("default_scope", "workspace")


def _timeout() -> float:
    return float(_config().get("timeout_seconds", 10))


def _key() -> str:
    """Resolve the Chronogram API key via Bitwarden SM (cached).

    The config value is a ``bitwarden:<secret-name>`` reference. There is no
    env-var or literal fallback — a bad reference raises.
    """
    global _api_key
    if _api_key is not None:
        return _api_key
    ref = _config()["api_key_secret_id"]
    if not isinstance(ref, str) or not ref.startswith("bitwarden:"):
        raise ChronogramError(
            "chronogram.http.api_key_secret_id must be a 'bitwarden:<name>' "
            f"reference (got {ref!r}); Bitwarden is the only secrets layer."
        )
    secret_name = ref.split(":", 1)[1]
    _api_key = get_secret(secret_name)  # raises BitwardenError if unreachable
    return _api_key


def _client() -> requests.Session:
    global _session
    if _session is None:
        with _lock:
            if _session is None:
                _session = requests.Session()
    return _session


def _request(method: str, path: str, *, json_body: dict | None = None) -> dict:
    """Perform an authenticated Chronogram request and return parsed JSON.

    Raises ChronogramError on any transport error or non-2xx response. No
    fallback path exists.
    """
    base = _config()["base_url"].rstrip("/")
    url = f"{base}{path}"
    headers = {"content-type": "application/json", "x-api-key": _key()}
    try:
        resp = _client().request(
            method, url, headers=headers, json=json_body, timeout=_timeout()
        )
    except requests.RequestException as e:
        raise ChronogramError(
            f"Chronogram {method} {path} failed to connect: {e}"
        ) from e
    if not (200 <= resp.status_code < 300):
        raise ChronogramError(
            f"Chronogram {method} {path} returned {resp.status_code}: "
            f"{resp.text[:300]}"
        )
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError as e:
        raise ChronogramError(
            f"Chronogram {method} {path} returned non-JSON body: {e}"
        ) from e


# ── Primitive ingest/recall/list ──────────────────────────────────────────


def _ingest(
    content: str,
    source: str,
    tags: list[str],
    type_hint: str = "episodic",
    source_id: str | None = None,
    observed_at: str | None = None,
) -> dict:
    payload: dict = {
        "scope": _scope(),
        "content": content,
        "source": source,
        "tags": tags,
        "typeHint": type_hint,
    }
    if source_id:
        payload["sourceId"] = source_id
    if observed_at:
        payload["observedAt"] = observed_at
    return _request("POST", "/v1/memories/ingest", json_body=payload)


def _list_workspace() -> list[dict]:
    data = _request("GET", f"/v1/memories?scope={_scope()}")
    return data.get("memories", []) or []


# ── Public artifact API (cross-agent memory) ──────────────────────────────


def store_artifact(namespace: str, content: str, metadata: dict | None = None) -> dict:
    """Store a memory artifact in the shared Chronogram under ``namespace``.

    Args:
        namespace: Logical grouping (e.g. "intel_feed-archive", "event_monitor-events").
            Recorded as a queryable tag.
        content: The artifact text. Stored verbatim so semantic recall works.
        metadata: Optional dict. Recognised keys: ``tags`` (list[str]),
            ``type_hint`` (str), ``source_id`` (str), ``source`` (str). Any
            other scalar entries are appended as ``key=value`` tags.

    Returns:
        The Chronogram ingest response dict (memoryId, accepted, ...).

    Raises:
        ChronogramError: on any connection/HTTP failure (no silent fallback).
    """
    metadata = metadata or {}
    tags = ["repose-artifact", f"ns:{namespace}", namespace]
    for t in metadata.get("tags", []) or []:
        tags.append(str(t))
    for k, v in metadata.items():
        if k in ("tags", "type_hint", "source_id", "source"):
            continue
        if isinstance(v, (str, int, float, bool)):
            tags.append(f"{k}={v}")
    logger.debug("Chronogram op=store_artifact ns=%s len=%d", namespace, len(content))
    return _ingest(
        content=content,
        source=metadata.get("source", f"repose:{namespace}"),
        tags=tags,
        type_hint=metadata.get("type_hint", "episodic"),
        source_id=metadata.get("source_id"),
    )


def query(namespace: str, query_text: str, limit: int = 20) -> list[dict]:
    """Recall artifacts from ``namespace`` matching ``query_text``.

    Returns the ranked ``context`` list of memory artifacts (each a dict).

    Raises:
        ChronogramError: on any connection/HTTP failure (no silent fallback).
    """
    payload = {
        "query": query_text,
        "scope": _scope(),
        "limit": limit,
        "filter": {"tags": [f"ns:{namespace}"]},
    }
    logger.debug("Chronogram op=query ns=%s q=%r", namespace, query_text[:60])
    data = _request("POST", "/v1/memories/recall", json_body=payload)
    return data.get("context", []) or []


# ── System-event API (backward compatible with the former stub) ───────────
#
# System events are stored as artifacts whose ``content`` is the JSON-encoded
# event dict, tagged for exact filtering. get_recent_events reconstructs the
# original dicts so existing callers (observer_core, intel_feed, event_monitor) are unchanged.


def log_system_event(
    namespace: str,
    agent: str,
    message_preview: str,
    rate_limited: bool = False,
    error: str | None = None,
    extra: dict | None = None,
) -> dict:
    """Log a system event to the shared Chronogram.

    Returns the event record as a dict (same shape as before). Raises
    ChronogramError if the event cannot be written — there is no in-memory
    fallback.
    """
    event: dict = {
        # event_id makes each logical event uniquely identifiable. Chronogram
        # fans one ingest out into several artifacts (working-memory +
        # semantic-store + ...), all carrying identical content; get_recent_events
        # dedups on this id so callers see one event, not N copies.
        "event_id": uuid.uuid4().hex,
        "namespace": namespace,
        "agent": agent,
        "message_preview": (message_preview or "")[:100],
        "rate_limited": rate_limited,
        "timestamp": time.time(),
        "timestamp_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if error:
        event["error"] = error
    if extra:
        event.update(extra)

    tags = ["repose-system-event", f"ns:{namespace}", f"agent:{agent}"]
    if error:
        tags.append("error")
    if rate_limited:
        tags.append("rate-limited")

    logger.debug(
        "Chronogram op=log_system_event ns=%s agent=%s rate_limited=%s error=%s",
        namespace, agent, rate_limited, error,
    )
    _ingest(
        content=json.dumps(event, default=str),
        source="repose-system-event",
        tags=tags,
        type_hint="episodic",
        observed_at=event["timestamp_iso"],
    )
    return event


def get_recent_events(
    namespace: str | None = None,
    agent: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Retrieve recent system events from the shared Chronogram.

    Reconstructs the original event dicts from artifact content, filtered by
    namespace/agent, most recent first. Raises ChronogramError on failure.
    """
    artifacts = _list_workspace()
    events: list[dict] = []
    seen: set[str] = set()
    for art in artifacts:
        tags = art.get("tags", []) or []
        if "repose-system-event" not in tags:
            continue
        if namespace and f"ns:{namespace}" not in tags:
            continue
        if agent and f"agent:{agent}" not in tags:
            continue
        try:
            ev = json.loads(art.get("content", ""))
        except (ValueError, TypeError):
            continue
        if not isinstance(ev, dict):
            continue
        # Dedup the multiple artifacts Chronogram creates per ingest.
        dedup_key = ev.get("event_id") or "{}|{}|{}|{}".format(
            ev.get("timestamp"), ev.get("namespace"),
            ev.get("agent"), ev.get("message_preview"),
        )
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        events.append(ev)
    events.sort(key=lambda e: e.get("timestamp", 0), reverse=True)
    return events[:limit]


def record_system_event(data: dict) -> dict:
    """Record a system event from a dict — canonical interface used by agents.

    Maps a data dict onto log_system_event.
    """
    return log_system_event(
        namespace=data.get("namespace", "system-events"),
        agent=data.get("agent", "unknown"),
        message_preview=data.get("message_preview", ""),
        rate_limited=data.get("rate_limited", False),
        error=data.get("error"),
        extra={k: v for k, v in data.items() if k not in (
            "namespace", "agent", "message_preview", "rate_limited", "error"
        )},
    )


def clear_events() -> None:
    """No-op retained for API compatibility.

    The Chronogram memory-api exposes no bulk-delete endpoint, so events cannot
    be cleared from the shared store. Previously this cleared a per-process
    in-memory buffer (the cross-agent memory bug). Test suites that relied on
    clearing should scope their assertions by tag/namespace instead.
    """
    logger.debug(
        "Chronogram clear_events() is a no-op against the shared memory-api."
    )
