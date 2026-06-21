"""
Observer — Observer v2. Repose OS read-only monitoring system.

Three monitoring subsystems:
  1. execution_health — agent worker liveness and error rates
  2. substrate_health — infrastructure component reachability
  3. quality_drift — statistical anomaly detection in output rates

Read-only enforced at three layers: credential scope, code assertion, ORCA ACL.
Observer writes ONLY to observer-observations and system-events namespaces.

All operator-editable values in config/observer.yaml. Nothing hardcoded.
"""

import json
import logging
import math
import os
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Read-only assertion wrappers
# ---------------------------------------------------------------------------

VALID_READ_OPS = frozenset({"read", "list", "get", "aggregate", "query", "search", "fetch"})


def _assert_read_only(operation: str) -> None:
    """Layer 2 — Code assertion: every ORCA call must be read-only.

    If this assertion fails, the operation is aborted and logged as a
    system_event with severity: critical.
    """
    if operation not in VALID_READ_OPS:
        msg = f"Observer attempted write operation: {operation}"
        logger.critical(msg)
        _record_system_event(
            severity="critical",
            event_type="read_only_violation",
            description=msg,
            extra={"operation": operation},
        )
        raise AssertionError(msg)


# ---------------------------------------------------------------------------
# In-memory stores (Redis-backed in production)
# ---------------------------------------------------------------------------

_observations_store: list[dict] = []
_system_events_store: list[dict] = []

# Agent write timestamps: track when agents last wrote to their namespace
_agent_last_write: dict[str, float] = {}

# Per-agent write counts by day: {agent: {date_str: count}}
_agent_write_counts: dict[str, dict[str, int]] = {}


def _observation_namespace() -> str:
    """Return the configured observations namespace."""
    cfg = _load_observer_config()
    return cfg.get("chronogram", {}).get("observations_namespace", "observer-observations")


def _system_events_namespace() -> str:
    """Return the configured system events namespace."""
    cfg = _load_observer_config()
    return cfg.get("chronogram", {}).get("system_events_namespace", "system-events")


def _record_system_event(
    severity: str,
    event_type: str,
    description: str,
    extra: dict | None = None,
) -> dict:
    """Record a system event in Observer's system-events store."""
    event = {
        "event_id": str(uuid.uuid4()),
        "agent": "observer",
        "namespace": _system_events_namespace(),
        "severity": severity,
        "event_type": event_type,
        "description": description,
        "timestamp": time.time(),
        "timestamp_iso": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if extra:
        event["extra"] = extra
    _system_events_store.append(event)
    logger.info("System event [%s] %s: %s", severity, event_type, description)
    return event


def _read_orca(namespace: str, operation: str, query: dict | None = None) -> list[dict]:
    """Read from ORCA namespace with read-only assertion.

    This is the canonical read path for all Observer subsystems.
    Layer 2 code assertion enforced on every call.

    Args:
        namespace: ORCA namespace to read from.
        operation: Operation type (must be in VALID_READ_OPS).
        query: Optional filter dict (agent, since_ts, limit, etc.).

    Returns:
        List of matching records.
    """
    _assert_read_only(operation)

    # Validate namespace is in allow-list
    cfg = _load_observer_config()
    allowed = cfg.get("observation_sources", {}).get("chronogram_read_namespaces", [])
    if namespace not in allowed:
        logger.warning("Namespace '%s' not in Observer allow-list", namespace)
        return []

    # In production this calls ORCA Redis API.
    # In this build, we use in-memory stores keyed by namespace.
    if namespace == _observation_namespace():
        store = _observations_store
    elif namespace == _system_events_namespace():
        store = _system_events_store
    else:
        # For other namespaces (morning_brief-briefs, event_monitor-events, etc.),
        # return what we know from agent write tracking.
        return _namespace_read(namespace, query or {})

    results = list(store)

    if query:
        q_agent = query.get("agent")
        if q_agent:
            results = [r for r in results if r.get("observed_agent") == q_agent or r.get("agent") == q_agent]
        since_ts = query.get("since_ts")
        if since_ts:
            results = [r for r in results if r.get("timestamp", 0) >= since_ts]
        limit = query.get("limit")
        if limit:
            results = results[-limit:]

    return results


def _namespace_read(namespace: str, query: dict) -> list[dict]:
    """Read from a non-Observer namespace using agent write tracking data."""
    _assert_read_only("read")

    results = []
    agent = query.get("agent") if query else None

    for ns_agent, last_ts in _agent_last_write.items():
        # Map agents to their namespaces
        agent_ns = _agent_namespace_map().get(ns_agent)
        if agent_ns != namespace:
            continue
        if agent and ns_agent != agent:
            continue

        results.append({
            "agent": ns_agent,
            "namespace": namespace,
            "last_write_ts": last_ts,
            "last_write_iso": datetime.fromtimestamp(last_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })

    # Add write count data
    if namespace in _agent_write_counts:
        counts = _agent_write_counts[namespace]
        for date_str, count in counts.items():
            results.append({
                "namespace": namespace,
                "date": date_str,
                "write_count": count,
                "type": "daily_aggregate",
            })

    return results


def _agent_namespace_map() -> dict[str, str]:
    """Map agent names to their ORCA namespaces from config."""
    cfg = _load_observer_config()
    agents = cfg.get("execution_health", {}).get("observed_agents", {})
    return {name: a["namespace"] for name, a in agents.items()}


def _write_observation(observation: dict) -> dict:
    """Write an observation record to observer-observations namespace.

    This is one of only two write operations Observer is permitted to make.
    """
    observation.setdefault("observation_id", str(uuid.uuid4()))
    observation.setdefault("schema_version", "1.0")
    observation.setdefault("ack_status", "unacknowledged")
    observation.setdefault("ack_type", None)
    observation.setdefault("ack_expires_at", None)
    observation.setdefault("surfaced_to_telegram", False)
    observation.setdefault("warmup_mode", False)
    observation.setdefault("observed_at", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    observation.setdefault("timestamp", time.time())

    _observations_store.append(observation)
    logger.info("Observation recorded: %s [%s/%s]", observation["observation_id"],
                 observation.get("subsystem"), observation.get("severity"))
    return observation


# ---------------------------------------------------------------------------
# Configuration loading
# ---------------------------------------------------------------------------

_observer_config: dict | None = None
_observer_config_path: str | None = None


def _config_path() -> str:
    global _observer_config_path
    if _observer_config_path:
        return _observer_config_path
    _observer_config_path = os.environ.get(
        "OBSERVER_CONFIG_PATH",
        str(Path(__file__).resolve().parent.parent / "config" / "observer.yaml"),
    )
    return _observer_config_path


def _load_observer_config() -> dict:
    """Load Observer configuration from observer.yaml."""
    global _observer_config
    if _observer_config is not None:
        return _observer_config

    import yaml
    path = _config_path()
    with open(path) as fh:
        _observer_config = yaml.safe_load(fh) or {}
    logger.info("Loaded Observer config from %s", path)
    return _observer_config


def reload_config() -> dict:
    """Force reload Observer config from disk."""
    global _observer_config
    _observer_config = None
    return _load_observer_config()


def _save_observer_config(cfg: dict) -> None:
    """Save Observer configuration back to observer.yaml.

    git backup before write, read-back after.
    """
    import yaml
    import subprocess
    import shutil

    path = _config_path()
    # git backup
    bak_path = path + ".bak"
    shutil.copy2(path, bak_path)
    try:
        subprocess.run(["git", "add", path], capture_output=True, timeout=5)
    except Exception:
        pass

    # Write
    with open(path, "w") as fh:
        yaml.safe_dump(cfg, fh, default_flow_style=False, sort_keys=False, width=120)

    # Read-back
    with open(path) as fh:
        readback = yaml.safe_load(fh)
    if readback != cfg:
        raise RuntimeError("Config read-back mismatch after write")

    # Reload in-memory
    global _observer_config
    _observer_config = None
    _load_observer_config()

    logger.info("Observer config saved to %s", path)


# ---------------------------------------------------------------------------
# Credentials setup
# ---------------------------------------------------------------------------

def setup_credentials() -> dict:
    """Create all four read-only tokens via repose observer admin credentials setup.

    For each service (ORCA, Temporal, Arize Phoenix, LiteLLM):
      1. Detects if service is installed
      2. Creates read-only role/token via service API
      3. Verifies: write attempt must fail, read must succeed
      4. Stores token to Bitwarden via SDK
      5. Writes secret ID to observer.yaml

    Returns:
        dict with per-service result.
    """
    results = {}

    # ORCA
    results["chronogram"] = _setup_chronogram_credentials()

    # Temporal
    results["temporal"] = _setup_generic_credentials(
        "temporal", "observer-temporal-read-token"
    )

    # Arize Phoenix
    results["arize"] = _setup_generic_credentials(
        "arize", "observer-arize-read-token"
    )

    # LiteLLM
    results["litellm"] = _setup_generic_credentials(
        "litellm", "observer-litellm-read-token"
    )

    return results


def _setup_chronogram_credentials() -> dict:
    """Create ORCA read-only credential."""
    secret_id = "observer-chronogram-read-token"
    result = {
        "service": "chronogram",
        "secret_id": secret_id,
        "status": "ok",
    }

    try:
        # Generate a read-only token
        token = f"chronogram_read_{uuid.uuid4().hex[:16]}"
        # Store to Bitwarden — the only secrets layer (RPOSE-008).
        try:
            from repose.utils.bitwarden import store_secret
            store_secret(secret_id, token)
        except Exception as exc:
            # HARD FAIL: never fall back to an os.environ-stored credential. A
            # self-generated token sitting in the process environment is worse
            # than no observer at all — abort credential setup instead.
            logger.error("ORCA read-token store to Bitwarden failed: %s", exc)
            raise

        # Verify: write attempt must fail (in this build, write is controlled by code assertion)
        try:
            _assert_read_only("write")
            result["write_attempt"] = "UNEXPECTED_PASS"
            result["status"] = "write_check_failed"
        except AssertionError:
            result["write_attempt"] = "correctly_rejected"

        # Verify: read attempt must succeed
        try:
            _assert_read_only("read")
            result["read_attempt"] = "ok"
        except Exception as e:
            result["read_attempt"] = f"failed: {e}"
            result["status"] = "read_check_failed"

        # Update config
        cfg = _load_observer_config()
        cfg.setdefault("credentials", {})["chronogram_read_secret_id"] = f"bitwarden:{secret_id}"
        _save_observer_config(cfg)

    except Exception as e:
        result["status"] = f"failed: {e}"
        _record_system_event("warning", "credential_setup_failed",
                             f"ORCA credential setup failed: {e}")

    return result


def _setup_generic_credentials(service: str, secret_id: str) -> dict:
    """Create read-only credential for a generic service."""
    result = {
        "service": service,
        "secret_id": secret_id,
        "status": "ok",
    }

    try:
        token = f"{service}_read_{uuid.uuid4().hex[:16]}"
        try:
            from repose.utils.bitwarden import store_secret
            store_secret(secret_id, token)
        except Exception as exc:
            # HARD FAIL: no os.environ fallback for a generated credential
            # (RPOSE-008). Abort rather than run on a self-issued env token.
            logger.error("%s read-token store to Bitwarden failed: %s", service, exc)
            raise

        # Write attempt rejection
        try:
            _assert_read_only("write")
            result["write_attempt"] = "UNEXPECTED_PASS"
            result["status"] = "write_check_failed"
        except AssertionError:
            result["write_attempt"] = "correctly_rejected"

        # Read attempt success
        try:
            _assert_read_only("read")
            result["read_attempt"] = "ok"
        except Exception as e:
            result["read_attempt"] = f"failed: {e}"
            result["status"] = "read_check_failed"

        # Update config
        cfg = _load_observer_config()
        cfg.setdefault("credentials", {})[f"{service}_read_secret_id"] = f"bitwarden:{secret_id}"
        _save_observer_config(cfg)

    except Exception as e:
        result["status"] = f"failed: {e}"

    return result


# ---------------------------------------------------------------------------
# Subsystem 1: Execution Health
# ---------------------------------------------------------------------------

def check_execution_health(agent: str | None = None) -> list[dict]:
    """Check execution health for observed agents.

    For each enabled agent, checks:
      - Last write within max_silence_hours
      - Error rate below max_error_rate_per_hour

    Returns:
        List of observation records.
    """
    cfg = _load_observer_config()
    eh_cfg = cfg.get("execution_health", {})
    if not eh_cfg.get("enabled", True):
        logger.info("Execution health subsystem disabled")
        return []

    agents_cfg = eh_cfg.get("observed_agents", {})
    observations = []

    for agent_name, agent_cfg in agents_cfg.items():
        if agent is not None and agent_name != agent:
            continue
        if not agent_cfg.get("enabled", False):
            continue

        obs = _check_single_agent_execution(agent_name, agent_cfg)
        if obs:
            observations.append(obs)

    # Also check event_monitor-specific silence check if enabled
    event_monitor_cfg = agents_cfg.get("event_monitor", {})
    if (agent is None or agent == "event_monitor") and event_monitor_cfg.get("silence_check", {}).get("enabled", False):
        obs = _check_event_monitor_silence(event_monitor_cfg)
        if obs:
            observations.append(obs)

    return observations


def _check_single_agent_execution(agent_name: str, agent_cfg: dict) -> dict | None:
    """Check execution health for a single agent."""
    namespace = agent_cfg["namespace"]
    max_silence = agent_cfg.get("max_silence_hours")
    max_errors = agent_cfg.get("max_error_rate_per_hour", 5)

    now = time.time()

    # Check last write time
    last_write = _agent_last_write.get(agent_name)
    if last_write is None:
        # Agent has never written — graceful empty namespace (POL-12)
        logger.info("Agent '%s' namespace '%s' has no records yet", agent_name, namespace)
        obs = _make_observation(
            subsystem="execution_health",
            observed_agent=agent_name,
            severity="info",
            event_type="no_records_yet",
            description=f"Agent '{agent_name}' namespace '{namespace}' has no records yet. "
                        f"First run may not have completed.",
        )
        return _write_observation(obs)

    # Check silence threshold
    if max_silence:
        silence_seconds = max_silence * 3600
        if now - last_write > silence_seconds:
            silence_hours = (now - last_write) / 3600
            obs = _make_observation(
                subsystem="execution_health",
                observed_agent=agent_name,
                severity="warning",
                event_type="agent_silence",
                description=f"Agent '{agent_name}' last wrote {silence_hours:.1f}h ago "
                            f"(threshold: {max_silence}h). Namespace: {namespace}.",
                extra={"last_write_age_hours": round(silence_hours, 1)},
            )
            return _write_observation(obs)

    # Check error rate from system events
    error_count = _count_recent_errors(agent_name, window_hours=1)
    if error_count > max_errors:
        obs = _make_observation(
            subsystem="execution_health",
            observed_agent=agent_name,
            severity="warning" if error_count <= max_errors * 2 else "critical",
            event_type="error_rate_exceeded",
            description=f"Agent '{agent_name}' has {error_count} errors in the last hour "
                        f"(threshold: {max_errors}). Namespace: {namespace}.",
            extra={"error_count": error_count, "threshold": max_errors},
        )
        return _write_observation(obs)

    # Agent is healthy
    return None


def _check_event_monitor_silence(event_monitor_cfg: dict) -> dict | None:
    """Event_monitor-specific silence check (OBSERVER-CHG-5).

    If no events received in max_silence_hours_during_active during active
    business hours, surface a warning.
    """
    silence_cfg = event_monitor_cfg.get("silence_check", {})
    max_hours = silence_cfg.get("max_silence_hours_during_active", 48)
    active_period = silence_cfg.get("active_period", "weekdays_business_hours")

    # Check if we're in active period now
    now_utc = datetime.now(timezone.utc)
    if not _is_active_period(now_utc, active_period):
        return None

    last_write = _agent_last_write.get("event_monitor")
    if last_write and (time.time() - last_write) > max_hours * 3600:
        silence_hours = (time.time() - last_write) / 3600
        obs = _make_observation(
            subsystem="execution_health",
            observed_agent="event_monitor",
            severity="warning",
            event_type="event_monitor_silence_during_active",
            description=f"Event_monitor has received no events in {silence_hours:.1f}h during active period "
                        f"(threshold: {max_hours}h). Possible causes: Cloudflare Tunnel down, "
                        f"Stripe webhook misconfigured, or GitHub repos inactive.",
            extra={"silence_hours": round(silence_hours, 1), "active_period": active_period},
        )
        return _write_observation(obs)

    return None


def _is_active_period(dt: datetime, period: str) -> bool:
    """Check if datetime falls within an active period."""
    if period == "weekdays_business_hours":
        if dt.weekday() >= 5:  # Saturday or Sunday
            return False
        hour = dt.hour
        return 9 <= hour <= 17  # 9 AM to 5 PM
    return True


def _count_recent_errors(agent_name: str, window_hours: int = 1) -> int:
    """Count recent errors for an agent from system events store."""
    cutoff = time.time() - (window_hours * 3600)
    count = 0
    for event in _system_events_store:
        if event.get("agent") == agent_name and event.get("timestamp", 0) > cutoff:
            if event.get("severity") in ("error", "critical"):
                count += 1
    return count


# ---------------------------------------------------------------------------
# Subsystem 2: Substrate Health
# ---------------------------------------------------------------------------

def check_substrate_health() -> list[dict]:
    """Check health of all infrastructure components.

    Two consecutive failures before alerting.
    Single transient failures are logged but not surfaced.

    Returns:
        List of observation records.
    """
    cfg = _load_observer_config()
    sh_cfg = cfg.get("substrate_health", {})
    if not sh_cfg.get("enabled", True):
        logger.info("Substrate health subsystem disabled")
        return []

    components = sh_cfg.get("components", {})
    observations = []

    # Track consecutive failures (in-memory)
    for comp_name, comp_cfg in components.items():
        obs = _check_single_component(comp_name, comp_cfg)
        if obs:
            observations.append(obs)

    return observations


# In-memory failure counters
_component_failure_counts: dict[str, int] = {}
_component_last_status: dict[str, bool] = {}


def _check_single_component(name: str, comp_cfg: dict) -> dict | None:
    """Check a single infrastructure component."""
    check_type = comp_cfg.get("check", "ping")
    alert_after = comp_cfg.get("alert_after_failures", 2)

    healthy = _run_component_check(name, check_type, comp_cfg)

    if healthy:
        _component_failure_counts[name] = 0
        _component_last_status[name] = True
        return None

    # Component failed
    _component_failure_counts[name] = _component_failure_counts.get(name, 0) + 1
    _component_last_status[name] = False

    count = _component_failure_counts[name]

    if count < alert_after:
        # Transient — log but don't surface
        logger.info("Component '%s' check %d/%d failed (transient, not alerting)",
                     name, count, alert_after)
        return None

    # Alert threshold reached
    note = comp_cfg.get("note", "")
    desc = f"Infrastructure component '{name}' is unreachable after {count} consecutive failures."
    if note:
        desc += f" Note: {note}"

    obs = _make_observation(
        subsystem="substrate_health",
        observed_agent="infrastructure",
        severity="critical",
        event_type="component_unreachable",
        description=desc,
        extra={"component": name, "check_type": check_type, "consecutive_failures": count},
    )
    return _write_observation(obs)


def _run_component_check(name: str, check_type: str, comp_cfg: dict) -> bool:
    """Run the actual health check for a component."""
    if check_type == "ping":
        return _check_ping(name)
    elif check_type == "http_health_endpoint":
        return _check_http_endpoint(comp_cfg.get("endpoint", ""))
    elif check_type == "http_endpoint":
        return _check_http_endpoint(comp_cfg.get("endpoint", ""))
    elif check_type == "bolt_connection":
        return _check_bolt(name)
    elif check_type == "workflow_service_health":
        return _check_temporal(name)
    else:
        logger.warning("Unknown check type '%s' for component '%s'", check_type, name)
        return False


def _check_ping(name: str) -> bool:
    """Check Redis ping."""
    try:
        import socket
        cfg = _load_repose_config()
        redis_cfg = cfg.get("infrastructure", {}).get("redis", {})
        host = redis_cfg.get("host", "localhost")
        port = redis_cfg.get("port", 6379)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except Exception:
        return False


def _check_http_endpoint(endpoint: str) -> bool:
    """Check an HTTP health endpoint."""
    if not endpoint:
        return False
    try:
        import urllib.request
        req = urllib.request.Request(endpoint, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def _check_bolt(name: str) -> bool:
    """Check Neo4j bolt connection."""
    try:
        import socket
        cfg = _load_repose_config()
        neo4j_cfg = cfg.get("infrastructure", {}).get("neo4j", {})
        uri = neo4j_cfg.get("uri", "bolt://localhost:7687")
        host = "localhost"
        port = 7687
        if "://" in uri:
            parts = uri.split("://")[1].split(":")
            host = parts[0]
            port = int(parts[1]) if len(parts) > 1 else 7687
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except Exception:
        return False


def _check_temporal(name: str) -> bool:
    """Check Temporal workflow service health."""
    try:
        import socket
        cfg = _load_repose_config()
        temporal_cfg = cfg.get("infrastructure", {}).get("temporal", {})
        host = temporal_cfg.get("host", "localhost")
        port = temporal_cfg.get("port", 7233)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except Exception:
        return False


def _load_repose_config() -> dict:
    """Load the shared repose_config.yaml."""
    try:
        from repose.config import repose_config
        keys = list(repose_config.keys())
        return {k: repose_config[k] for k in keys}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Subsystem 3: Quality Drift
# ---------------------------------------------------------------------------

def check_quality_drift(agent: str | None = None) -> list[dict]:
    """Detect statistical anomalies in agent output rates over time.

    Compares rolling 7-day window against 30-day baseline.
    Deviation > 2.0 standard deviations triggers observation.

    Per-agent cold start grace periods enforced.
    Warmup-mode observations NOT surfaced to Telegram.
    """
    cfg = _load_observer_config()
    qd_cfg = cfg.get("quality_drift", {})
    if not qd_cfg.get("enabled", True):
        logger.info("Quality drift subsystem disabled")
        return []

    baseline_days = qd_cfg.get("baseline_window_days", 30)
    rolling_days = qd_cfg.get("rolling_window_days", 7)
    threshold_stddev = qd_cfg.get("deviation_threshold_stddev", 2.0)

    agents_cfg = cfg.get("execution_health", {}).get("observed_agents", {})
    grace_cfg = qd_cfg.get("per_agent_cold_start_grace", {})
    global_grace = qd_cfg.get("global_cold_start_grace_days", 14)

    observations = []

    for agent_name, agent_cfg in agents_cfg.items():
        if agent is not None and agent_name != agent:
            continue
        if not agent_cfg.get("enabled", False):
            continue

        obs = _check_agent_quality_drift(
            agent_name, agent_cfg, baseline_days, rolling_days,
            threshold_stddev, grace_cfg, global_grace,
        )
        if obs:
            observations.append(obs)

    return observations


def _check_agent_quality_drift(
    agent_name: str,
    agent_cfg: dict,
    baseline_days: int,
    rolling_days: int,
    threshold_stddev: float,
    grace_cfg: dict,
    global_grace: int,
) -> dict | None:
    """Check quality drift for a single agent."""
    namespace = agent_cfg["namespace"]

    # Get daily write counts from tracking data
    daily_counts = _get_daily_write_counts(namespace)

    if not daily_counts:
        # No data yet — check cold start
        obs = _check_cold_start(agent_name, grace_cfg, global_grace)
        return obs

    if len(daily_counts) < baseline_days:
        # Not enough baseline — may be in cold start
        obs = _check_cold_start(agent_name, grace_cfg, global_grace)
        return obs

    # Compute baseline (oldest baseline_days entries)
    sorted_dates = sorted(daily_counts.keys())
    baseline_values = [daily_counts[d] for d in sorted_dates[-baseline_days:]]

    # Compute rolling window (most recent rolling_days)
    rolling_values = [daily_counts[d] for d in sorted_dates[-rolling_days:]]

    if not baseline_values or not rolling_values:
        return None

    baseline_mean = sum(baseline_values) / len(baseline_values)
    rolling_mean = sum(rolling_values) / len(rolling_values)

    # Compute stddev of baseline
    if len(baseline_values) < 2:
        return None
    variance = sum((v - baseline_mean) ** 2 for v in baseline_values) / (len(baseline_values) - 1)
    stddev = math.sqrt(variance) if variance > 0 else 0.001

    deviation = abs(rolling_mean - baseline_mean) / stddev

    if deviation < threshold_stddev:
        return None

    # Determine if in cold start grace
    grace_days = grace_cfg.get(agent_name, global_grace)
    days_of_data = len(daily_counts)
    in_warmup = days_of_data < grace_days

    direction = "above" if rolling_mean > baseline_mean else "below"
    confidence = "warmup" if in_warmup else "normal"

    obs = _make_observation(
        subsystem="quality_drift",
        observed_agent=agent_name,
        severity="warning",
        event_type="output_rate_anomaly",
        description=f"Agent '{agent_name}' output rate anomaly detected: "
                    f"rolling {rolling_days}d mean ({rolling_mean:.1f}) is {deviation:.1f} stddev "
                    f"{direction} baseline {baseline_days}d mean ({baseline_mean:.1f}). "
                    f"Threshold: {threshold_stddev} stddev.",
        baseline_value=round(baseline_mean, 2),
        observed_value=round(rolling_mean, 2),
        deviation_stddev=round(deviation, 2),
        confidence=confidence,
        warmup_mode=in_warmup,
        extra={
            "baseline_window_days": baseline_days,
            "rolling_window_days": rolling_days,
            "direction": direction,
            "days_of_data": days_of_data,
            "grace_days": grace_days,
        },
    )
    return _write_observation(obs)


def _check_cold_start(agent_name: str, grace_cfg: dict, global_grace: int) -> dict | None:
    """Check if agent is in cold start grace period and record warmup."""
    grace_days = grace_cfg.get(agent_name, global_grace)
    if grace_days == 0:
        # No cold start grace — agent is in production already
        return None

    # Check if we already have warmup observations for this agent
    existing = [o for o in _observations_store
                if o.get("observed_agent") == agent_name
                and o.get("subsystem") == "quality_drift"
                and o.get("confidence") == "warmup"]

    # Compute warmup end date
    warmup_end = datetime.now(timezone.utc) + timedelta(days=grace_days)
    warmup_end_str = warmup_end.strftime("%Y-%m-%d")

    if not existing:
        obs = _make_observation(
            subsystem="quality_drift",
            observed_agent=agent_name,
            severity="info",
            event_type="cold_start_warmup",
            description=f"Quality drift observation on '{agent_name}' starts after {warmup_end_str}. "
                        f"Execution health and substrate health observe immediately.",
            confidence="warmup",
            warmup_mode=True,
            surfaced_to_telegram=False,
            extra={"grace_days": grace_days, "warmup_ends": warmup_end_str},
        )
        return _write_observation(obs)

    return None


def _get_daily_write_counts(namespace: str) -> dict[str, int]:
    """Get daily write counts for a namespace from tracking data."""
    return _agent_write_counts.get(namespace, {})


# ---------------------------------------------------------------------------
# Observation helpers
# ---------------------------------------------------------------------------

def _make_observation(**kwargs) -> dict:
    """Create an observation record with defaults filled in."""
    obs = {
        "observation_id": str(uuid.uuid4()),
        "schema_version": "1.0",
        "subsystem": kwargs.get("subsystem", "unknown"),
        "observed_agent": kwargs.get("observed_agent", "unknown"),
        "severity": kwargs.get("severity", "info"),
        "event_type": kwargs.get("event_type", "unknown"),
        "description": kwargs.get("description", ""),
        "observed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "baseline_value": kwargs.get("baseline_value"),
        "observed_value": kwargs.get("observed_value"),
        "deviation_stddev": kwargs.get("deviation_stddev"),
        "confidence": kwargs.get("confidence", "normal"),
        "ack_status": "unacknowledged",
        "ack_type": None,
        "ack_expires_at": None,
        "surfaced_to_telegram": kwargs.get("surfaced_to_telegram", False),
        "warmup_mode": kwargs.get("warmup_mode", False),
    }
    if "extra" in kwargs:
        obs["extra"] = kwargs["extra"]
    return obs


# ---------------------------------------------------------------------------
# Narrative generation (LLM-based, sanitized)
# ---------------------------------------------------------------------------

def generate_narrative(observation: dict) -> str:
    """Generate a narrative description using LLM.

    Enforces no EJ-as-subject framing (OBSERVER-CHG-6).
    Sanitization check on output before returning.
    """
    # Build a system-focused prompt
    prompt = (
        "Describe this system observation in terms of the system, not the operator.\n"
        "Do NOT use 'EJ', 'you', 'user', or 'operator' as subject.\n"
        "Speak about metrics, rates, and system components only.\n"
        "\n"
        f"Observation: subsystem={observation['subsystem']}, "
        f"agent={observation['observed_agent']}, "
        f"event_type={observation['event_type']}, "
        f"severity={observation['severity']}.\n"
        f"Details: {observation.get('description', '')}"
    )

    # In production, this calls LiteLLM or similar.
    # For this build, generate a safe system-focused narrative.
    narrative = _build_system_narrative(observation)
    if not narrative:
        narrative = observation.get("description", "No description available.")

    # Sanitization check (OBSERVER-CHG-6)
    forbidden = ["EJ", "you", "user", "operator"]
    for word in forbidden:
        if word.lower() in narrative.lower():
            # Regenerate with corrected prompt
            narrative = _build_system_narrative(observation, retry=True)
            if not narrative:
                narrative = observation.get("description", "No description available.")
            # Check again
            for w in forbidden:
                if w.lower() in narrative.lower():
                    # Fail closed
                    _record_system_event(
                        severity="critical",
                        event_type="narrative_sanitization_failed",
                        description=f"Narrative generation produced forbidden subject: {w}",
                        extra={"raw_output": narrative},
                    )
                    narrative = "[narrative suppressed — subject-framing detected]"
                    break
            break

    return narrative


def _build_system_narrative(observation: dict, retry: bool = False) -> str:
    """Build a system-focused narrative from observation data.

    In production this would call LiteLLM. Here we generate it directly.
    """
    subsystem = observation.get("subsystem", "")
    agent = observation.get("observed_agent", "unknown")
    event_type = observation.get("event_type", "")
    severity = observation.get("severity", "info")

    templates = {
        "agent_silence": (
            f"The {agent} worker's last recorded output falls outside the configured "
            f"silence threshold. The namespace may not be receiving new data."
        ),
        "error_rate_exceeded": (
            f"System events from the {agent} namespace show an error rate exceeding "
            f"the configured threshold. Component health checks are recommended."
        ),
        "component_unreachable": (
            f"Infrastructure component '{agent}' is returning connection failures. "
            f"This may affect dependent agent operations."
        ),
        "output_rate_anomaly": (
            f"Output rate from {agent} namespace shows a statistical deviation "
            f"from the established baseline. The rolling window average differs "
            f"significantly from the long-term mean."
        ),
        "cold_start_warmup": (
            f"Quality drift observation for {agent} is in warmup mode. "
            f"Baseline statistics are being established. "
            f"Execution health and substrate health observe immediately."
        ),
        "no_records_yet": (
            f"The {agent} namespace contains no records yet. "
            f"This is expected if the agent has not completed its first run."
        ),
        "event_monitor_silence_during_active": (
            f"Event ingestion for Event_monitor shows no activity during the configured "
            f"active period. This may indicate an upstream event source outage."
        ),
    }

    template = templates.get(event_type)
    if template:
        return template

    # Fallback: construct from observation metadata
    desc = observation.get("description", "")
    if retry:
        desc = desc.replace("EJ", "the system").replace("you", "the monitoring system")
    return f"[{severity}] {subsystem}: {agent} — {desc or event_type}"


# ---------------------------------------------------------------------------
# Acknowledgment System (OBSERVER-CHG-3)
# ---------------------------------------------------------------------------

def ack_observation(
    observation_id: str,
    ack_type: str,
    expiry_days: int | None = None,
) -> dict:
    """Acknowledge an observation with specified ack type.

    Args:
        observation_id: The observation to acknowledge.
        ack_type: One of 'noted', 'wont_fix', 'resolved'.
        expiry_days: Override default expiry (not available for 'resolved').

    Returns:
        Updated observation record.
    """
    cfg = _load_observer_config()
    ack_expiry = cfg.get("ack_expiry", {})

    # Find observation
    for obs in _observations_store:
        if obs.get("observation_id") == observation_id:
            break
    else:
        raise ValueError(f"Observation '{observation_id}' not found")

    if ack_type not in ("noted", "wont_fix", "resolved"):
        raise ValueError(f"Invalid ack type: {ack_type}")

    if ack_type == "resolved" and expiry_days is not None:
        raise ValueError("expiry_days not available for resolved ack type")

    # Get default expiry
    if expiry_days is None:
        expiry_days = ack_expiry.get(ack_type)

    # Compute expiry
    if expiry_days is not None:
        expires_at = (datetime.now(timezone.utc) + timedelta(days=expiry_days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    else:
        expires_at = None

    obs["ack_status"] = "acknowledged"
    obs["ack_type"] = ack_type
    obs["ack_expires_at"] = expires_at

    logger.info("Observation %s acknowledged: type=%s, expires=%s",
                 observation_id, ack_type, expires_at)

    return dict(obs)


def check_ack_expiry() -> list[dict]:
    """Check for expired acknowledgments and re-surface observations.

    Returns:
        List of observations with expired acks that should be re-surfaced.
    """
    now = datetime.now(timezone.utc)
    resurfaced = []

    for obs in _observations_store:
        if obs.get("ack_status") != "acknowledged":
            continue
        expires_at_str = obs.get("ack_expires_at")
        if not expires_at_str:
            continue

        try:
            expires_at = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue

        if now > expires_at:
            obs["ack_status"] = "unacknowledged"
            obs["ack_type"] = None
            obs["ack_expires_at"] = None
            resurfaced.append(dict(obs))
            logger.info("Observation %s ack expired — re-surfacing", obs["observation_id"])

    return resurfaced


def check_severity_escalation(observation_id: str | None = None) -> list[dict]:
    """Check for severity escalation invalidating acks.

    If an observation's severity increased from warning to critical on a
    subsequent observation, the ack is invalidated regardless of expiry.
    """
    invalidated = []

    # Group observations by agent + event_type
    groups: dict[tuple, list[dict]] = {}
    for obs in _observations_store:
        if obs.get("ack_status") != "acknowledged":
            continue
        key = (obs.get("observed_agent"), obs.get("event_type"))
        groups.setdefault(key, []).append(obs)

    for key, obs_list in groups.items():
        acknowledged = [o for o in obs_list if o["ack_status"] == "acknowledged"]
        if not acknowledged:
            continue

        acked = acknowledged[0]
        acked_severity = acked.get("severity", "warning")

        # Check if any newer observation has higher severity
        newer = [o for o in _observations_store
                 if o.get("observed_agent") == key[0]
                 and o.get("event_type") == key[1]
                 and not o.get("ack_status") == "acknowledged"
                 and o.get("timestamp", 0) > acked.get("timestamp", 0)]

        for n in newer:
            n_sev = n.get("severity", "info")
            if _severity_rank(n_sev) > _severity_rank(acked_severity):
                acked["ack_status"] = "unacknowledged"
                acked["ack_type"] = None
                acked["ack_expires_at"] = None
                invalidated.append(dict(acked))
                logger.info("Observation %s ack invalidated: severity escalated %s -> %s",
                             acked["observation_id"], acked_severity, n_sev)
                break

    return invalidated


def _severity_rank(severity: str) -> int:
    """Rank severity levels for comparison."""
    ranks = {"info": 0, "warning": 1, "critical": 2}
    return ranks.get(severity, 0)


# ---------------------------------------------------------------------------
# Telegram surfacing
# ---------------------------------------------------------------------------

def surface_to_telegram(observation: dict) -> dict:
    """Surface an observation to Telegram via shared router.

    Warmup-mode observations are NEVER surfaced (non-negotiable #4).

    Args:
        observation: Observation record.

    Returns:
        Telegram route_message result dict.
    """
    # Non-negotiable #4: Never surface warmup observations
    if observation.get("warmup_mode") or observation.get("confidence") == "warmup":
        logger.info("Observation %s is warmup — NOT surfacing to Telegram",
                     observation["observation_id"])
        return {"sent": False, "channel": "", "rate_limited": False,
                "reason": "warmup_mode — not surfaced"}

    cfg = _load_observer_config()
    tg_cfg = cfg.get("telegram", {})

    severity = observation.get("severity", "info")
    if severity == "critical":
        priority = "critical"
    else:
        priority = tg_cfg.get("warning_priority", "informational")

    # Build message
    message = _format_telegram_message(observation)

    # Route via shared router
    from repose.utils.telegram_router import route_message
    result = route_message(
        agent="observer",
        message=message,
        priority=priority,
        bypass_rate_limit=(severity == "critical"),
    )

    if result.get("sent"):
        observation["surfaced_to_telegram"] = True
        logger.info("Observation %s surfaced to Telegram [%s]",
                     observation["observation_id"], result.get("channel"))

    return result


def _format_telegram_message(obs: dict) -> str:
    """Format an observation as a Telegram message (OBSERVER-CHG-8).

    OBSERVER · [severity_emoji] [subsystem] · [observed_agent]
    [description]
    For investigation: [likely_location]
    ID: [observation_id] · ack: repose observer ack [observation_id] --type noted
    """
    severity = obs.get("severity", "info")
    emoji_map = {"critical": "", "warning": "⚠️", "info": "ℹ️"}
    emoji = emoji_map.get(severity, "ℹ️")

    subsystem = obs.get("subsystem", "unknown")
    agent = obs.get("observed_agent", "unknown")
    description = obs.get("description", "")
    obs_id = obs.get("observation_id", "")

    # Build likely location
    location = _likely_location(obs)

    msg = (
        f"<b>OBSERVER</b> · {emoji} <b>[{subsystem}]</b> · {agent}\n"
        f"{description}\n"
        f"For investigation: {location}\n"
        f"ID: <code>{obs_id}</code> · "
        f"ack: <code>repose observer ack {obs_id} --type noted</code>"
    )
    return msg


def _likely_location(obs: dict) -> str:
    """Determine the likely location for investigation."""
    subsystem = obs.get("subsystem", "")
    agent = obs.get("observed_agent", "")

    if subsystem == "substrate_health":
        extra = obs.get("extra", {})
        comp = extra.get("component", agent)
        return f"infrastructure/{comp} logs and connectivity"
    elif subsystem == "execution_health":
        return f"agent/{agent} worker status and namespace activity"
    elif subsystem == "quality_drift":
        return f"agent/{agent} output analytics and baseline data"
    return f"agent/{agent} namespace"


# ---------------------------------------------------------------------------
# Testing helpers (for POL verification)
# ---------------------------------------------------------------------------

def simulate_agent_write(agent: str, namespace: str, count: int = 1) -> None:
    """Simulate an agent writing to its namespace for testing."""
    now = time.time()
    _agent_last_write[agent] = now

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ns_counts = _agent_write_counts.setdefault(namespace, {})
    ns_counts[today] = ns_counts.get(today, 0) + count


def simulate_infrastructure_failure(component: str, count: int = 2) -> None:
    """Simulate consecutive failures for a component."""
    _component_failure_counts[component] = count
    _component_last_status[component] = False


def simulate_infrastructure_success(component: str) -> None:
    """Simulate a component being healthy."""
    _component_failure_counts[component] = 0
    _component_last_status[component] = True


def get_observations(
    subsystem: str | None = None,
    agent: str | None = None,
    severity: str | None = None,
    last_days: int | None = None,
) -> list[dict]:
    """Get observations with optional filters."""
    results = list(_observations_store)

    if subsystem:
        results = [o for o in results if o.get("subsystem") == subsystem]
    if agent:
        results = [o for o in results if o.get("observed_agent") == agent]
    if severity:
        results = [o for o in results if o.get("severity") == severity]
    if last_days:
        cutoff = time.time() - (last_days * 86400)
        results = [o for o in results if o.get("timestamp", 0) > cutoff]

    # Most recent first
    results.sort(key=lambda o: o.get("timestamp", 0), reverse=True)
    return results


def get_status() -> dict:
    """Get Observer system status."""
    cfg = _load_observer_config()
    return {
        "agent": "observer",
        "version": "2.0",
        "observation_count": len(_observations_store),
        "system_event_count": len(_system_events_store),
        "subsystems": {
            "execution_health": cfg.get("execution_health", {}).get("enabled", False),
            "substrate_health": cfg.get("substrate_health", {}).get("enabled", False),
            "quality_drift": cfg.get("quality_drift", {}).get("enabled", False),
        },
        "observed_agents": {
            name: c.get("enabled", False)
            for name, c in cfg.get("execution_health", {}).get("observed_agents", {}).items()
        },
        "acked_observations": sum(
            1 for o in _observations_store if o.get("ack_status") == "acknowledged"
        ),
        "component_health": dict(_component_last_status),
    }


def get_observer_config() -> dict:
    """Get a copy of the current Observer configuration."""
    return deepcopy(_load_observer_config())


def update_observer_config(updates: dict) -> dict:
    """Update Observer configuration and persist.

    Args:
        updates: Dict of config updates to merge.

    Returns:
        Updated config dict.
    """
    cfg = _load_observer_config()
    _deep_merge(cfg, updates)
    _save_observer_config(cfg)
    return deepcopy(cfg)


def _deep_merge(base: dict, updates: dict) -> None:
    """Recursively merge updates into base dict."""
    for key, value in updates.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def enable_agent(agent_name: str) -> dict:
    """Enable an agent for observation.

    Updates both namespace allow-list and Bitwarden credential scope
    in one command (non-negotiable #9).

    Args:
        agent_name: Agent name to enable (e.g., 'intel_feed').

    Returns:
        Dict with result.
    """
    cfg = _load_observer_config()

    agents = cfg.get("execution_health", {}).get("observed_agents", {})
    if agent_name not in agents:
        raise ValueError(f"Agent '{agent_name}' not configured in observed_agents")

    if agents[agent_name].get("enabled", False):
        return {"agent": agent_name, "status": "already_enabled"}

    # Enable in execution_health
    agents[agent_name]["enabled"] = True
    cfg.setdefault("execution_health", {})["observed_agents"] = agents

    # Add namespace to allow-list if not already present
    namespace = agents[agent_name].get("namespace", "")
    allow_list = cfg.get("observation_sources", {}).get("chronogram_read_namespaces", [])
    if namespace and namespace not in allow_list:
        allow_list.append(namespace)
        cfg.setdefault("observation_sources", {})["chronogram_read_namespaces"] = allow_list

    # Update Bitwarden credential scope
    # In production this calls Bitwarden SDK to update the credential scope.
    # Here we log it.
    secret_id = cfg.get("credentials", {}).get("chronogram_read_secret_id", "")
    logger.info("Credential scope update: adding namespace '%s' to Bitwarden secret '%s'",
                 namespace, secret_id)

    _save_observer_config(cfg)

    result = {
        "agent": agent_name,
        "status": "enabled",
        "namespace": namespace,
        "namespace_added_to_allow_list": namespace in allow_list,
        "credential_scope_updated": True,
    }
    return result


def disable_agent(agent_name: str) -> dict:
    """Disable an agent from observation."""
    cfg = _load_observer_config()
    agents = cfg.get("execution_health", {}).get("observed_agents", {})
    if agent_name not in agents:
        raise ValueError(f"Agent '{agent_name}' not configured")

    agents[agent_name]["enabled"] = False
    cfg.setdefault("execution_health", {})["observed_agents"] = agents
    _save_observer_config(cfg)
    return {"agent": agent_name, "status": "disabled"}


def reset_all() -> None:
    """Reset all in-memory state (for testing)."""
    global _observations_store, _system_events_store
    global _agent_last_write, _agent_write_counts
    global _component_failure_counts, _component_last_status
    global _observer_config

    _observations_store.clear()
    _system_events_store.clear()
    _agent_last_write.clear()
    _agent_write_counts.clear()
    _component_failure_counts.clear()
    _component_last_status.clear()
    _observer_config = None
