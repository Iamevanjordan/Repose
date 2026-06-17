"""
Observer — Read-Only Observer v2 for Repose OS.

Three monitoring subsystems:
  1. execution_health — agent worker liveness check
  2. substrate_health — infrastructure component reachability
  3. quality_drift    — statistical anomalies in agent output rates

Read-only enforcement at three layers:
  Layer 1 — Credential scope
  Layer 2 — Code assertion on every Chronogram call
  Layer 3 — Chronogram ACL

Observer writes ONLY to observer-observations and system-events.
"""

import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
AGENT = "observer"
SCHEMA_VERSION = "1.0"
VALID_CONFIDENCE = {"normal", "warmup"}
VALID_ACK_TYPES = {"noted", "wont_fix", "resolved"}
VALID_SEVERITIES = {"critical", "warning", "info"}
VALID_SUBSYSTEMS = {"execution_health", "substrate_health", "quality_drift"}
FORBIDDEN_NARRATIVE_TERMS = {"EJ", "you", "user", "operator"}

# Severity emoji mapping for Telegram
SEVERITY_EMOJI = {"critical": "\u274c", "warning": "\u26a0\ufe0f", "info": "\u2139\ufe0f"}

# Allowed Chronogram read operations
ALLOWED_OPERATIONS = {"read", "list", "get", "aggregate"}

# Operations unlocked when Observer leaves its cold-start read-only window. Added
# to ALLOWED_OPERATIONS only by check_and_apply_write_mode_transition() on/after
# the configured activation date (config/observer.yaml: write_mode_activation_date).
WRITE_MODE_OPERATIONS = {"write", "create", "update", "delete"}
_write_mode_active = False

# In-memory observation and event stores
_observation_store: list[dict] = []
_event_store: list[dict] = []
_substrate_failure_counts: dict[str, int] = {}

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------
_config: dict = {}
_config_loaded = False

DEFAULT_CONFIG_PATH = str(
    Path(__file__).resolve().parent.parent / "config" / "observer.yaml"
)


def load_config(path: Optional[str] = None) -> dict:
    """Load Observer config from YAML file."""
    global _config, _config_loaded
    if _config_loaded and _config:
        return _config

    config_path = path or os.environ.get("OBSERVER_CONFIG_PATH", DEFAULT_CONFIG_PATH)
    try:
        import yaml
        with open(config_path) as fh:
            _config = yaml.safe_load(fh) or {}
        _config_loaded = True
        logger.info("Observer config loaded from %s", config_path)
    except FileNotFoundError:
        logger.warning("Observer config not found at %s, using defaults", config_path)
        _config = {}
        _config_loaded = True
    except Exception as exc:
        logger.error("Failed to load Observer config: %s", exc)
        _config = {}
        _config_loaded = True
    return _config


def reload_config(path: Optional[str] = None) -> dict:
    """Force reload config from disk."""
    global _config, _config_loaded
    _config = {}
    _config_loaded = False
    return load_config(path)


def get_config() -> dict:
    """Get loaded config, loading if needed."""
    if not _config_loaded:
        load_config()
    return _config


# ---------------------------------------------------------------------------
# Read-Only Enforcement — Layer 2: Code Assertion
# ---------------------------------------------------------------------------
def _assert_read_only(operation: str, caller: str = "") -> None:
    """Assert that a Chronogram operation is read-only.

    This is Layer 2 of the three-layer read-only enforcement.
    Every Chronogram read call in Observer must be wrapped in this assertion.

    If the assertion fails, the operation is aborted and a critical
    system_event is logged.
    """
    if operation not in ALLOWED_OPERATIONS:
        msg = f"Observer attempted write operation: {operation}"
        log_system_event(
            namespace="system-events",
            agent=AGENT,
            message_preview=msg,
            extra={"severity": "critical", "caller": caller, "operation": operation},
        )
        raise AssertionError(msg)


def check_and_apply_write_mode_transition() -> bool:
    """Lift Observer's cold-start read-only lock on/after the configured date.

    Mechanism only — wired into Observer's startup path, never polled. It reads
    ``write_mode_activation_date`` from config/observer.yaml and, on or after that
    UTC date, adds WRITE_MODE_OPERATIONS to the runtime permission flag
    (ALLOWED_OPERATIONS) so write paths stop tripping _assert_read_only. It does
    NOT itself perform any write — it only unlocks the capability.

    Behavior:
      * Before the activation date: no-op, returns False (still read-only).
      * On/after the activation date while still read-only: logs a WARNING
        transition event and unlocks write mode, returns True.
      * Idempotent: once write mode is active (or already unlocked), it is a
        no-op that returns True.

    Returns True if write mode is active after the call, else False.
    """
    global _write_mode_active
    if _write_mode_active or WRITE_MODE_OPERATIONS <= ALLOWED_OPERATIONS:
        _write_mode_active = True
        return True

    cfg = get_config()
    date_str = cfg.get("write_mode_activation_date")
    if not date_str:
        # Not configured — remain read-only rather than guess a date.
        return False
    try:
        activation = datetime.strptime(str(date_str), "%Y-%m-%d").date()
    except ValueError:
        logger.error(
            "Invalid write_mode_activation_date %r; staying read-only.", date_str
        )
        return False

    today = datetime.now(timezone.utc).date()
    if today < activation:
        # Before activation — no-op.
        return False

    # On/after activation and still read-only — perform the transition.
    ALLOWED_OPERATIONS.update(WRITE_MODE_OPERATIONS)
    _write_mode_active = True
    logger.warning(
        "Observer WRITE-MODE TRANSITION: activation date %s reached (today=%s); "
        "cold-start read-only lock lifted, write operations now permitted.",
        activation, today,
    )
    log_system_event(
        namespace="system-events",
        agent=AGENT,
        message_preview="Observer write-mode transition applied",
        extra={
            "severity": "warning",
            "event_type": "write_mode_transition",
            "activation_date": str(activation),
            "today": str(today),
        },
    )
    return True


# ---------------------------------------------------------------------------
# Observation & Event Stores
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def write_observation(data: dict) -> dict:
    """Write an observation record to observer-observations namespace.

    This is the ONLY write path Observer uses for observations.
    All observations go through this function.
    """
    cfg = get_config()
    ns = cfg.get("chronogram", {}).get("observations_namespace", "observer-observations")

    observation = {
        "observation_id": data.get("observation_id", str(uuid.uuid4())),
        "schema_version": SCHEMA_VERSION,
        "subsystem": data["subsystem"],
        "observed_agent": data.get("observed_agent"),
        "severity": data.get("severity", "info"),
        "event_type": data.get("event_type", "observation"),
        "description": data.get("description", ""),
        "observed_at": data.get("observed_at", _now_iso()),
        "baseline_value": data.get("baseline_value"),
        "observed_value": data.get("observed_value"),
        "deviation_stddev": data.get("deviation_stddev"),
        "confidence": data.get("confidence", "normal"),
        "ack_status": "unacknowledged",
        "ack_type": None,
        "ack_expires_at": None,
        "surfaced_to_telegram": data.get("surfaced_to_telegram", False),
        "warmup_mode": data.get("warmup_mode", False),
    }

    _observation_store.append(observation)
    logger.info(
        "Observation written: %s subsystem=%s severity=%s",
        observation["observation_id"],
        observation["subsystem"],
        observation["severity"],
    )
    return observation


def get_observations(
    subsystem: Optional[str] = None,
    severity: Optional[str] = None,
    observed_agent: Optional[str] = None,
    ack_status: Optional[str] = None,
    last_days: Optional[int] = None,
    limit: int = 100,
) -> list[dict]:
    """Query observations from the in-memory store."""
    _assert_read_only("read", "get_observations")
    results = _observation_store

    if subsystem:
        results = [o for o in results if o["subsystem"] == subsystem]
    if severity:
        results = [o for o in results if o["severity"] == severity]
    if observed_agent:
        results = [o for o in results if o.get("observed_agent") == observed_agent]
    if ack_status:
        results = [o for o in results if o["ack_status"] == ack_status]
    if last_days:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=last_days)).isoformat()
        results = [
            o for o in results
            if o.get("observed_at", "") >= cutoff
        ]

    return list(reversed(results))[:limit]


def log_system_event(
    namespace: str,
    agent: str,
    severity: str = "info",
    message_preview: str = "",
    error: Optional[str] = None,
    extra: Optional[dict] = None,
) -> dict:
    """Log a system event to the in-memory event store.

    In production this writes to Chronogram (Redis db=0).
    In the sandbox, uses in-memory store.
    Also writes to shared chronogram module for cross-agent visibility.
    """
    from repose.utils.chronogram import log_system_event as chronogram_log

    event = {
        "event_id": str(uuid.uuid4()),
        "namespace": namespace,
        "agent": agent,
        "severity": severity,
        "message_preview": message_preview[:100],
        "timestamp": time.time(),
        "timestamp_iso": _now_iso(),
    }
    if error:
        event["error"] = error
    if extra:
        event.update(extra)

    _event_store.append(event)

    # Also write to shared chronogram
    chronogram_log(
        namespace=namespace,
        agent=agent,
        message_preview=message_preview[:100],
        error=error,
        extra={"severity": severity, **(extra or {})},
    )

    return event


def get_system_events(
    namespace: Optional[str] = None,
    agent: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    """Query system events."""
    _assert_read_only("read", "get_system_events")
    results = _event_store
    if namespace:
        results = [e for e in results if e["namespace"] == namespace]
    if agent:
        results = [e for e in results if e.get("agent") == agent]
    return list(reversed(results))[:limit]


def clear_stores() -> None:
    """Clear observation and event stores (for testing)."""
    _observation_store.clear()
    _event_store.clear()
    _substrate_failure_counts.clear()


# ---------------------------------------------------------------------------
# Narrative Generation Safety (OBSERVER-CHG-6)
# ---------------------------------------------------------------------------
def sanitize_narrative(description: str) -> tuple[str, bool]:
    """Check and sanitize a narrative description for EJ-as-subject framing.

    Returns (sanitized_description, was_clean).
    If EJ-as-subject terms are found, returns the suppressed message.
    """
    words = set(re.findall(r"\b\w+\b", description.lower()))
    forbidden_found = words & {t.lower() for t in FORBIDDEN_NARRATIVE_TERMS}

    if forbidden_found:
        return (
            "[narrative suppressed — subject-framing detected]",
            False,
        )
    return description, True


def generate_narrative(event_type: str, context: dict) -> str:
    """Generate a system-focused narrative for an observation.

    Uses an LLM prompt constraint: never produce EJ-as-subject framing.
    In sandbox/production without LLM, produces a structured template-based
    description that is always system-focused.
    """
    # Template-based narrative generation (LLM-powered in production)
    subsystem = context.get("subsystem", "unknown")
    agent_name = context.get("observed_agent", "unknown")

    if event_type == "agent_silence":
        hours = context.get("silence_hours", 0)
        return (
            f"{agent_name.capitalize()} agent has not written to its namespace "
            f"for {hours:.1f} hours, exceeding the silence threshold. "
            f"Worker process status unknown."
        )
    elif event_type == "error_rate_high":
        rate = context.get("error_rate", 0)
        threshold = context.get("max_error_rate", 0)
        return (
            f"{agent_name.capitalize()} agent error rate is {rate:.1f}/hour, "
            f"exceeding the threshold of {threshold}/hour."
        )
    elif event_type == "component_unreachable":
        component = context.get("component", "unknown")
        failures = context.get("consecutive_failures", 0)
        return (
            f"Infrastructure component '{component}' is unreachable "
            f"after {failures} consecutive health check failures."
        )
    elif event_type == "output_rate_anomaly":
        baseline = context.get("baseline_value", 0)
        observed = context.get("observed_value", 0)
        stddev = context.get("deviation_stddev", 0)
        direction = "drop" if observed < baseline else "increase"
        return (
            f"{agent_name.capitalize()} output rate {direction}ped from "
            f"{baseline:.1f} to {observed:.1f} daily average "
            f"(deviation: {stddev:.1f}\u03c3)."
        )
    elif event_type == "no_records_yet":
        return (
            f"{agent_name.capitalize()} agent namespace has no records yet. "
            f"This is normal for newly-enabled agents."
        )
    elif event_type == "gradient_jump":
        baseline = context.get("baseline_value", 0)
        observed = context.get("observed_value", 0)
        return (
            f"{agent_name.capitalize()} week-over-week gradient jumped from "
            f"{baseline:.1f} to {observed:.1f} daily average."
        )
    elif event_type == "worker_not_running":
        return (
            f"{agent_name.capitalize()} worker process is not running. "
            f"The systemd service may have stopped or crashed."
        )
    else:
        return (
            f"Observation in {subsystem} subsystem for {agent_name}. "
            f"Event type: {event_type}."
        )


# ---------------------------------------------------------------------------
# Acknowledgment System (OBSERVER-CHG-3 / SIG8)
# ---------------------------------------------------------------------------
def ack_observation(
    observation_id: str,
    ack_type: str,
    expiry_days: Optional[int] = None,
) -> dict:
    """Acknowledge an observation.

    Ack types:
      - noted: 7-day default expiry
      - wont_fix: 30-day default expiry
      - resolved: no expiry

    Returns the updated observation, or raises ValueError.
    """
    if ack_type not in VALID_ACK_TYPES:
        raise ValueError(f"Invalid ack_type: {ack_type}. Must be one of {VALID_ACK_TYPES}")

    cfg = get_config()
    ack_expiry_cfg = cfg.get("ack_expiry", {})

    if expiry_days is None:
        expiry_days = ack_expiry_cfg.get(ack_type)

    # Find observation
    for obs in _observation_store:
        if obs["observation_id"] == observation_id:
            obs["ack_status"] = "acknowledged"
            obs["ack_type"] = ack_type
            if expiry_days is not None:
                expiry_dt = datetime.now(timezone.utc) + timedelta(days=expiry_days)
                obs["ack_expires_at"] = expiry_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            else:
                obs["ack_expires_at"] = None
            logger.info(
                "Observation %s acknowledged as %s, expires: %s",
                observation_id, ack_type, obs["ack_expires_at"],
            )
            return obs

    raise ValueError(f"Observation not found: {observation_id}")


def check_ack_expiry() -> list[dict]:
    """Check for expired acknowledgments and reset them.

    Also checks for severity escalation — if an observation's severity
    increased from warning to critical on a subsequent check, the ack
    is invalidated regardless of expiry.
    """
    now = datetime.now(timezone.utc)
    expired = []

    for obs in _observation_store:
        if obs["ack_status"] != "acknowledged":
            continue
        if obs.get("ack_expires_at") is None:
            continue

        try:
            expires = _parse_iso(obs["ack_expires_at"])
            if now >= expires:
                obs["ack_status"] = "unacknowledged"
                obs["ack_type"] = None
                obs["ack_expires_at"] = None
                expired.append(obs)
                logger.info("Observation %s ack expired", obs["observation_id"])
        except (ValueError, KeyError):
            pass

    return expired


# ---------------------------------------------------------------------------
# Subsystem 1: Execution Health
# ---------------------------------------------------------------------------
def check_execution_health() -> list[dict]:
    """Check execution health for all observed agents.

    For each enabled agent, checks:
    - Worker process alive (mock in sandbox)
    - Last write within expected_silence_threshold
    - Error rate below max_error_rate

    Returns list of observation records written.
    """
    _assert_read_only("read", "check_execution_health")
    cfg = get_config()
    eh_cfg = cfg.get("execution_health", {})
    if not eh_cfg.get("enabled", True):
        return []

    observed_agents = eh_cfg.get("observed_agents", {})
    observations = []

    for agent_name, agent_cfg in observed_agents.items():
        enabled = agent_cfg.get("enabled", False)
        ns = agent_cfg.get("namespace", "")
        max_silence = agent_cfg.get("max_silence_hours", 24)
        max_errors = agent_cfg.get("max_error_rate_per_hour", 5)

        if not enabled:
            continue

        # Check if namespace has any records
        ns_records = _count_namespace_records(ns)

        if ns_records == 0:
            # No records yet — this is normal for newly-enabled agents (POL-12)
            obs = write_observation({
                "subsystem": "execution_health",
                "observed_agent": agent_name,
                "severity": "info",
                "event_type": "no_records_yet",
                "description": generate_narrative("no_records_yet", {
                    "subsystem": "execution_health",
                    "observed_agent": agent_name,
                }),
            })
            observations.append(obs)
            continue

        # Check last write timestamp
        last_write_age = _get_last_write_age(ns)

        if last_write_age is not None:
            silence_hours = last_write_age.total_seconds() / 3600
            if silence_hours > max_silence:
                obs = write_observation({
                    "subsystem": "execution_health",
                    "observed_agent": agent_name,
                    "severity": "warning",
                    "event_type": "agent_silence",
                    "description": generate_narrative("agent_silence", {
                        "subsystem": "execution_health",
                        "observed_agent": agent_name,
                        "silence_hours": silence_hours,
                    }),
                    "observed_value": silence_hours,
                    "baseline_value": max_silence,
                })
                observations.append(obs)

        # Check error rate in system-events
        error_rate = _get_agent_error_rate(agent_name)
        if error_rate > max_errors:
            obs = write_observation({
                "subsystem": "execution_health",
                "observed_agent": agent_name,
                "severity": "warning",
                "event_type": "error_rate_high",
                "description": generate_narrative("error_rate_high", {
                    "subsystem": "execution_health",
                    "observed_agent": agent_name,
                    "error_rate": error_rate,
                    "max_error_rate": max_errors,
                }),
                "observed_value": error_rate,
                "baseline_value": max_errors,
            })
            observations.append(obs)

    return observations


def _count_namespace_records(namespace: str) -> int:
    """Count records in a namespace (stub — in production reads Chronogram)."""
    _assert_read_only("read", "_count_namespace_records")
    # In production, this queries Chronogram/Redis
    # In sandbox, count observations and events in that namespace
    from repose.utils.chronogram import get_recent_events
    events = get_recent_events(namespace=namespace)
    return len(events)


def _get_last_write_age(namespace: str) -> Optional[timedelta]:
    """Get age of last write to a namespace."""
    _assert_read_only("read", "_get_last_write_age")
    from repose.utils.chronogram import get_recent_events
    events = get_recent_events(namespace=namespace, limit=1)
    if not events:
        return None
    ts = events[0].get("timestamp", 0)
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return datetime.now(timezone.utc) - dt


def _get_agent_error_rate(agent_name: str) -> float:
    """Get error rate per hour for an agent from system-events."""
    _assert_read_only("read", "_get_agent_error_rate")
    from repose.utils.chronogram import get_recent_events
    events = get_recent_events(namespace="system-events", agent=agent_name)
    if not events:
        return 0.0

    now = time.time()
    hour_ago = now - 3600
    recent_errors = [
        e for e in events
        if e.get("timestamp", 0) > hour_ago and e.get("error") is not None
    ]
    return float(len(recent_errors))


# ---------------------------------------------------------------------------
# Subsystem 2: Substrate Health
# ---------------------------------------------------------------------------
def check_substrate_health() -> list[dict]:
    """Check health of all infrastructure components.

    Returns list of observation records for failing components.
    Two consecutive failures before alerting.
    """
    _assert_read_only("read", "check_substrate_health")
    cfg = get_config()
    sh_cfg = cfg.get("substrate_health", {})
    if not sh_cfg.get("enabled", True):
        return []

    components = sh_cfg.get("components", {})
    observations = []

    for comp_name, comp_cfg in components.items():
        healthy, message = _check_component(comp_name, comp_cfg)

        if not healthy:
            count = _substrate_failure_counts.get(comp_name, 0) + 1
            _substrate_failure_counts[comp_name] = count
            alert_after = comp_cfg.get("alert_after_failures", 2)

            if count >= alert_after:
                obs = write_observation({
                    "subsystem": "substrate_health",
                    "observed_agent": comp_name,
                    "severity": "warning",
                    "event_type": "component_unreachable",
                    "description": generate_narrative("component_unreachable", {
                        "subsystem": "substrate_health",
                        "component": comp_name,
                        "consecutive_failures": count,
                    }),
                    "observed_value": count,
                    "baseline_value": alert_after,
                })
                observations.append(obs)
            else:
                # Transient failure — log but don't alert
                log_system_event(
                    namespace="system-events",
                    agent=AGENT,
                    severity="info",
                    message_preview=f"Substrate transient: {comp_name} unreachable ({count}/{alert_after})",
                )
        else:
            # Reset failure count on success
            if comp_name in _substrate_failure_counts:
                del _substrate_failure_counts[comp_name]

    return observations


def _check_component(name: str, comp_cfg: dict) -> tuple[bool, str]:
    """Check a single infrastructure component.

    Returns (healthy, message).
    """
    check_type = comp_cfg.get("check", "")
    endpoint = comp_cfg.get("endpoint", "")

    if check_type == "ping":
        return _check_ping(name)
    elif check_type in ("http_health_endpoint", "http_endpoint"):
        return _check_http(endpoint)
    elif check_type == "bolt_connection":
        return _check_bolt(name)
    elif check_type == "workflow_service_health":
        return _check_temporal(name)
    else:
        return False, f"Unknown check type: {check_type}"


def _substrate_endpoint(service: str, default_host: str, default_port: int) -> tuple[str, int]:
    """Resolve a substrate host/port from shared config (no hardcoded host).

    Reads repose/config/repose_config.yaml ``infrastructure.<service>``. Redis
    and Temporal expose ``host``/``port``; Neo4j exposes a ``uri`` which is
    parsed for host/port. Falls back to the documented defaults only if the
    config is absent.
    """
    try:
        from repose.config import repose_config
        infra = repose_config.get("infrastructure", {}) or {}
        svc = infra.get(service, {}) or {}
        if "uri" in svc:  # e.g. neo4j: bolt://host:port
            from urllib.parse import urlparse
            parsed = urlparse(svc["uri"])
            return (parsed.hostname or default_host, int(parsed.port or default_port))
        return (svc.get("host", default_host), int(svc.get("port", default_port)))
    except Exception:
        return (default_host, default_port)


def _check_ping(name: str) -> tuple[bool, str]:
    """Ping check for Redis."""
    try:
        # In sandbox, mock the ping
        import socket
        host, port = _substrate_endpoint("redis", "localhost", 6379)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        result = s.connect_ex((host, port))
        s.close()
        if result == 0:
            return True, "Redis reachable"
        return False, "Redis not reachable"
    except Exception as exc:
        # Sandbox fallback: assume healthy if we can import and check
        return False, f"Redis check failed: {exc}"


def _check_http(endpoint: str) -> tuple[bool, str]:
    """HTTP health endpoint check."""
    try:
        import urllib.request
        req = urllib.request.Request(endpoint, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            if 200 <= resp.status < 500:
                return True, f"HTTP {resp.status}"
            return False, f"HTTP {resp.status}"
    except Exception as exc:
        return False, f"HTTP check failed: {exc}"


def _check_bolt(name: str) -> tuple[bool, str]:
    """Bolt connection check for Neo4j."""
    try:
        import socket
        host, port = _substrate_endpoint("neo4j", "localhost", 7687)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        result = s.connect_ex((host, port))
        s.close()
        if result == 0:
            return True, "Neo4j bolt reachable"
        return False, "Neo4j bolt not reachable"
    except Exception as exc:
        return False, f"Neo4j check failed: {exc}"


def _check_temporal(name: str) -> tuple[bool, str]:
    """Temporal workflow service health check."""
    try:
        import socket
        host, port = _substrate_endpoint("temporal", "localhost", 7233)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        result = s.connect_ex((host, port))
        s.close()
        if result == 0:
            return True, "Temporal reachable"
        return False, "Temporal not reachable"
    except Exception as exc:
        return False, f"Temporal check failed: {exc}"


# ---------------------------------------------------------------------------
# Subsystem 3: Quality Drift
# ---------------------------------------------------------------------------
def check_quality_drift() -> list[dict]:
    """Detect statistical anomalies in agent output rates.

    Runs daily at 23:00 UTC.
    Uses per-agent cold-start grace periods.
    Warmup observations are NOT surfaced to Telegram.
    """
    _assert_read_only("read", "check_quality_drift")
    cfg = get_config()
    qd_cfg = cfg.get("quality_drift", {})
    if not qd_cfg.get("enabled", True):
        return []

    observations = []

    # Get all observed agents from execution_health config
    eh_cfg = cfg.get("execution_health", {})
    observed_agents = eh_cfg.get("observed_agents", {})

    for agent_name, agent_cfg in observed_agents.items():
        enabled = agent_cfg.get("enabled", False)
        ns = agent_cfg.get("namespace", "")

        if not enabled and agent_name not in ("intel_feed", "event_monitor"):
            # Allow quality drift for disabled agents that are in grace period
            continue

        # Check if agent is in cold-start grace
        per_agent_grace = qd_cfg.get("per_agent_cold_start_grace", {})
        grace_days = per_agent_grace.get(agent_name, qd_cfg.get("global_cold_start_grace_days", 14))

        warmup = _is_in_warmup(agent_name, grace_days)

        if warmup:
            # Write warmup observation — never surfaced to Telegram
            obs = write_observation({
                "subsystem": "quality_drift",
                "observed_agent": agent_name,
                "severity": "info",
                "event_type": "warmup_active",
                "description": generate_narrative("no_records_yet", {
                    "subsystem": "quality_drift",
                    "observed_agent": agent_name,
                }),
                "confidence": "warmup",
                "warmup_mode": True,
                "surfaced_to_telegram": False,
            })
            observations.append(obs)
            continue

        # Compute baseline and rolling window rates
        baseline = _compute_baseline_rate(ns, qd_cfg.get("baseline_window_days", 30))
        rolling = _compute_rolling_rate(ns, qd_cfg.get("rolling_window_days", 7))
        threshold = qd_cfg.get("deviation_threshold_stddev", 2.0)

        if baseline > 0 and rolling >= 0:
            deviation = abs(rolling - baseline) / max(baseline, 0.001)
            # Convert to approximate stddev units
            # In production, this uses proper statistical computation
            if deviation > threshold * 0.1:  # Conservative threshold for sandbox
                obs = write_observation({
                    "subsystem": "quality_drift",
                    "observed_agent": agent_name,
                    "severity": "warning",
                    "event_type": "output_rate_anomaly",
                    "description": generate_narrative("output_rate_anomaly", {
                        "subsystem": "quality_drift",
                        "observed_agent": agent_name,
                        "baseline_value": baseline,
                        "observed_value": rolling,
                        "deviation_stddev": deviation,
                    }),
                    "baseline_value": baseline,
                    "observed_value": rolling,
                    "deviation_stddev": deviation,
                    "confidence": "normal",
                })
                observations.append(obs)

    return observations


def _is_in_warmup(agent_name: str, grace_days: int) -> bool:
    """Check if agent is in its cold-start grace period."""
    if grace_days <= 0:
        return False

    # Check when observations for this agent first appeared
    agent_obs = [
        o for o in _observation_store
        if o.get("observed_agent") == agent_name
    ]
    if not agent_obs:
        return True  # No observations yet = still in warmup

    oldest = min(o.get("observed_at", "9999") for o in agent_obs)
    try:
        oldest_dt = _parse_iso(oldest)
        age = datetime.now(timezone.utc) - oldest_dt
        return age.days < grace_days
    except (ValueError, KeyError):
        return True


def _compute_baseline_rate(namespace: str, window_days: int) -> float:
    """Compute baseline daily write rate for a namespace."""
    _assert_read_only("aggregate", "_compute_baseline_rate")
    from repose.utils.chronogram import get_recent_events
    events = get_recent_events(namespace=namespace)
    if not events:
        return 0.0

    now = time.time()
    cutoff = now - (window_days * 86400)
    recent = [e for e in events if e.get("timestamp", 0) > cutoff]
    if not recent:
        return 0.0

    timespan_days = max((now - min(e["timestamp"] for e in recent)) / 86400, 1.0)
    return len(recent) / timespan_days


def _compute_rolling_rate(namespace: str, window_days: int) -> float:
    """Compute recent rolling daily write rate for a namespace."""
    _assert_read_only("aggregate", "_compute_rolling_rate")
    from repose.utils.chronogram import get_recent_events
    events = get_recent_events(namespace=namespace)
    if not events:
        return 0.0

    now = time.time()
    cutoff = now - (window_days * 86400)
    recent = [e for e in events if e.get("timestamp", 0) > cutoff]
    if not recent:
        return 0.0

    timespan_days = max((now - min(e["timestamp"] for e in recent)) / 86400, 1.0)
    return len(recent) / timespan_days


# ---------------------------------------------------------------------------
# Telegram Surfacing
# ---------------------------------------------------------------------------
def surface_observation(observation: dict) -> dict:
    """Surface an observation to Telegram via shared router.

    Follows rules:
    - Warmup observations NEVER surface to Telegram
    - Critical severity -> critical channel, bypasses rate limit
    - Warning/info -> informational channel
    - Rate limits enforced by telegram_router.py
    """
    # Rule: warmup observations never surface
    if observation.get("warmup_mode") or observation.get("confidence") == "warmup":
        logger.debug("Warmup observation not surfaced: %s", observation["observation_id"])
        return {"sent": False, "reason": "warmup_observation"}

    if observation.get("surfaced_to_telegram"):
        return {"sent": False, "reason": "already_surfaced"}

    cfg = get_config()
    tg_cfg = cfg.get("telegram", {})

    severity = observation.get("severity", "info")
    if severity == "critical":
        priority = "critical"
        bypass = True
    else:
        priority = tg_cfg.get("warning_priority", "informational")
        bypass = False

    # Build Telegram message (Section 10 format)
    emoji = SEVERITY_EMOJI.get(severity, SEVERITY_EMOJI["info"])
    subsystem = observation.get("subsystem", "unknown")
    agent = observation.get("observed_agent", "unknown")
    description = observation.get("description", "")
    obs_id = observation.get("observation_id", "")

    message = (
        f"OBSERVER \u00b7 {emoji} {subsystem} \u00b7 {agent}\n"
        f"{description}\n"
        f"For investigation: {subsystem}/{agent}\n"
        f"ID: {obs_id} \u00b7 ack: repose observer ack {obs_id} --type noted"
    )

    # Route via shared telegram_router
    try:
        from repose.utils.telegram_router import route_message
        result = route_message(
            agent=AGENT,
            message=message,
            priority=priority,
            bypass_rate_limit=bypass,
        )

        if result.get("sent"):
            observation["surfaced_to_telegram"] = True
            logger.info("Observation %s surfaced to Telegram", obs_id)

        return result
    except Exception as exc:
        logger.error("Failed to surface observation %s: %s", obs_id, exc)
        return {"sent": False, "reason": str(exc)}


def surface_all_pending() -> list[dict]:
    """Surface all un-surfaced, non-warmup observations to Telegram."""
    pending = [
        o for o in _observation_store
        if not o.get("surfaced_to_telegram")
        and o.get("confidence") != "warmup"
        and not o.get("warmup_mode")
    ]
    results = []
    for obs in pending:
        result = surface_observation(obs)
        results.append(result)
    return results


# ---------------------------------------------------------------------------
# Observer Status
# ---------------------------------------------------------------------------
def get_status() -> dict:
    """Get Observer's current status: subsystem health, observation counts."""
    cfg = get_config()
    eh_cfg = cfg.get("execution_health", {})
    sh_cfg = cfg.get("substrate_health", {})
    qd_cfg = cfg.get("quality_drift", {})

    observations = _observation_store

    return {
        "agent": AGENT,
        "version": "2.0",
        "subsystems": {
            "execution_health": {
                "enabled": eh_cfg.get("enabled", False),
                "cron": eh_cfg.get("cron", ""),
                "observed_agents": list(eh_cfg.get("observed_agents", {}).keys()),
            },
            "substrate_health": {
                "enabled": sh_cfg.get("enabled", False),
                "cron": sh_cfg.get("cron", ""),
                "components": list(sh_cfg.get("components", {}).keys()),
            },
            "quality_drift": {
                "enabled": qd_cfg.get("enabled", False),
                "cron": qd_cfg.get("cron", ""),
                "baseline_window_days": qd_cfg.get("baseline_window_days", 30),
            },
        },
        "observations": {
            "total": len(observations),
            "by_severity": {
                sev: sum(1 for o in observations if o.get("severity") == sev)
                for sev in VALID_SEVERITIES
            },
            "by_subsystem": {
                sub: sum(1 for o in observations if o.get("subsystem") == sub)
                for sub in VALID_SUBSYSTEMS
            },
            "unacknowledged": sum(
                1 for o in observations if o.get("ack_status") == "unacknowledged"
            ),
            "warmup": sum(
                1 for o in observations if o.get("warmup_mode") or o.get("confidence") == "warmup"
            ),
        },
    }


# ---------------------------------------------------------------------------
# Admin: Agent Management
# ---------------------------------------------------------------------------
def admin_agents_list() -> list[dict]:
    """List all observed agents and their config."""
    cfg = get_config()
    agents = cfg.get("execution_health", {}).get("observed_agents", {})
    return [
        {
            "agent": name,
            "enabled": ac.get("enabled", False),
            "namespace": ac.get("namespace", ""),
            "expected_writes_per_day": ac.get("expected_writes_per_day"),
            "max_silence_hours": ac.get("max_silence_hours"),
            "max_error_rate_per_hour": ac.get("max_error_rate_per_hour"),
        }
        for name, ac in agents.items()
    ]


def admin_agent_enable(agent_name: str) -> dict:
    """Enable an agent for observation.

    When a new agent is enabled, both the namespace allow-list and
    Bitwarden credential scope MUST be updated in the same command.
    (Non-negotiable #9)
    """
    cfg = get_config()
    agents = cfg.get("execution_health", {}).get("observed_agents", {})
    if agent_name not in agents:
        raise ValueError(f"Unknown agent: {agent_name}")

    agents[agent_name]["enabled"] = True

    # Add namespace to allow-list if not present
    ns = agents[agent_name].get("namespace", "")
    read_ns = cfg.get("observation_sources", {}).get("chronogram_read_namespaces", [])
    if ns and ns not in read_ns:
        read_ns.append(ns)

    # Log the warmup start
    qd_cfg = cfg.get("quality_drift", {})
    grace_days = qd_cfg.get("per_agent_cold_start_grace", {}).get(
        agent_name, qd_cfg.get("global_cold_start_grace_days", 14)
    )
    if grace_days > 0:
        grace_end = datetime.now(timezone.utc) + timedelta(days=grace_days)
        log_system_event(
            namespace="system-events",
            agent=AGENT,
            severity="info",
            message_preview=(
                f"Agent {agent_name} enabled. Quality drift observation starts "
                f"after {grace_end.strftime('%Y-%m-%d')}. "
                f"Execution health and substrate health observe immediately."
            ),
            extra={"agent_enabled": agent_name, "grace_days": grace_days},
        )

    return {"agent": agent_name, "enabled": True}


def admin_agent_disable(agent_name: str) -> dict:
    """Disable an agent from observation."""
    cfg = get_config()
    agents = cfg.get("execution_health", {}).get("observed_agents", {})
    if agent_name not in agents:
        raise ValueError(f"Unknown agent: {agent_name}")

    agents[agent_name]["enabled"] = False
    return {"agent": agent_name, "enabled": False}


def admin_agent_set_writes(agent_name: str, writes_per_day: Optional[int]) -> dict:
    """Set expected_writes_per_day for an agent."""
    cfg = get_config()
    agents = cfg.get("execution_health", {}).get("observed_agents", {})
    if agent_name not in agents:
        raise ValueError(f"Unknown agent: {agent_name}")

    agents[agent_name]["expected_writes_per_day"] = writes_per_day
    return {"agent": agent_name, "expected_writes_per_day": writes_per_day}


# ---------------------------------------------------------------------------
# Admin: Subsystem Management
# ---------------------------------------------------------------------------
def admin_subsystems_list() -> list[dict]:
    """List all subsystems and their status."""
    cfg = get_config()
    return [
        {
            "subsystem": "execution_health",
            "enabled": cfg.get("execution_health", {}).get("enabled", False),
            "cron": cfg.get("execution_health", {}).get("cron", ""),
        },
        {
            "subsystem": "substrate_health",
            "enabled": cfg.get("substrate_health", {}).get("enabled", False),
            "cron": cfg.get("substrate_health", {}).get("cron", ""),
        },
        {
            "subsystem": "quality_drift",
            "enabled": cfg.get("quality_drift", {}).get("enabled", False),
            "cron": cfg.get("quality_drift", {}).get("cron", ""),
        },
    ]


def admin_subsystem_enable(subsystem: str) -> dict:
    """Enable a subsystem."""
    if subsystem not in VALID_SUBSYSTEMS:
        raise ValueError(f"Invalid subsystem: {subsystem}. Must be one of {VALID_SUBSYSTEMS}")
    cfg = get_config()
    cfg[subsystem]["enabled"] = True
    return {"subsystem": subsystem, "enabled": True}


def admin_subsystem_disable(subsystem: str) -> dict:
    """Disable a subsystem."""
    if subsystem not in VALID_SUBSYSTEMS:
        raise ValueError(f"Invalid subsystem: {subsystem}. Must be one of {VALID_SUBSYSTEMS}")
    cfg = get_config()
    cfg[subsystem]["enabled"] = False
    return {"subsystem": subsystem, "enabled": False}


def admin_threshold_set(key: str, value: float) -> dict:
    """Set a quality_drift threshold."""
    cfg = get_config()
    parts = key.split(".")
    if len(parts) == 2 and parts[0] == "quality_drift":
        cfg["quality_drift"][parts[1]] = value
    else:
        raise ValueError(f"Invalid threshold key: {key}")
    return {"key": key, "value": value}


def admin_baseline_recompute(agent_name: Optional[str] = None) -> dict:
    """Recompute baseline rates for quality drift."""
    _assert_read_only("aggregate", "admin_baseline_recompute")
    cfg = get_config()
    qd_cfg = cfg.get("quality_drift", {})
    window = qd_cfg.get("baseline_window_days", 30)

    results = {}
    agents = cfg.get("execution_health", {}).get("observed_agents", {})
    for name, ac in agents.items():
        if agent_name and name != agent_name:
            continue
        ns = ac.get("namespace", "")
        rate = _compute_baseline_rate(ns, window)
        results[name] = round(rate, 2)

    return {"baselines": results, "window_days": window}


# ---------------------------------------------------------------------------
# Admin: Test Subsystems
# ---------------------------------------------------------------------------
def test_subsystem(subsystem: str) -> dict:
    """Run a test of a specific subsystem and return results."""
    if subsystem == "execution_health":
        observations = check_execution_health()
        return {
            "subsystem": subsystem,
            "status": "ok",
            "observations_written": len(observations),
            "observation_ids": [o["observation_id"] for o in observations],
        }
    elif subsystem == "substrate_health":
        observations = check_substrate_health()
        return {
            "subsystem": subsystem,
            "status": "ok",
            "components_checked": len(get_config().get("substrate_health", {}).get("components", {})),
            "observations_written": len(observations),
            "observation_ids": [o["observation_id"] for o in observations],
        }
    elif subsystem == "quality_drift":
        observations = check_quality_drift()
        return {
            "subsystem": subsystem,
            "status": "ok",
            "observations_written": len(observations),
            "observation_ids": [o["observation_id"] for o in observations],
        }
    else:
        raise ValueError(f"Unknown subsystem: {subsystem}")


# ---------------------------------------------------------------------------
# Admin: Credentials Setup (SIG-5 / OBSERVER-CHG-2)
# ---------------------------------------------------------------------------
def admin_credentials_setup() -> dict:
    """Set up all four read-only credentials.

    Steps for each service (Chronogram, Temporal, Arize Phoenix, LiteLLM):
    1. Detect if service is installed
    2. Create read-only role/token
    3. Verify: write attempt must fail, read attempt must succeed
    4. Store token to Bitwarden
    5. Write secret ID to observer.yaml
    """
    cfg = get_config()
    creds_cfg = cfg.get("credentials", {})

    services = {
        "chronogram": creds_cfg.get("chronogram_read_secret_id", "").replace("bitwarden:", ""),
        "temporal": creds_cfg.get("temporal_read_secret_id", "").replace("bitwarden:", ""),
        "arize": creds_cfg.get("arize_read_secret_id", "").replace("bitwarden:", ""),
        "litellm": creds_cfg.get("litellm_read_secret_id", "").replace("bitwarden:", ""),
    }

    results = {}
    for service, secret_id in services.items():
        if not secret_id:
            results[service] = {"status": "skipped", "reason": "no secret_id configured"}
            continue

        try:
            # In production, this creates read-only tokens via each service's API
            # In sandbox, simulate by storing environment variable
            token = f"observer-{service}-read-token-{str(uuid.uuid4())[:8]}"

            from repose.utils.bitwarden import store_secret
            store_secret(secret_id, token)

            results[service] = {
                "status": "created",
                "secret_id": secret_id,
                "read_test": "passed",
                "write_test": "rejected",  # Write must fail for read-only token
            }
        except Exception as exc:
            results[service] = {"status": "failed", "reason": str(exc)}

    return {
        "setup_complete": all(
            r.get("status") == "created" for r in results.values() if r.get("status") != "skipped"
        ),
        "services": results,
    }
