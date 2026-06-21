"""
Event_monitor v3 — Repose OS Event Watcher

Webhook receiver + event classification pipeline:
  1. Receive webhooks via HTTP (Cloudflare Tunnel fronted)
  2. Validate signatures per-source
  3. Deduplicate (Redis db=3, with in-memory fallback)
  4. Sanitize payloads
  5. Classify via LLM into 4 routing lanes
  6. Route to ORCA namespaces
  7. Surface to Telegram via shared telegram_router.py

Config-driven: all operator values from event_monitor.yaml. Nothing hardcoded.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import signal
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Optional

import yaml

from repose.utils.daemon import DaemonGuard

logger = logging.getLogger("event_monitor")

# ── Module-level state ──────────────────────────────────────────────────
_config: dict = {}
_server: Optional[HTTPServer] = None
_event_store: list[dict] = []          # In-memory ORCA fallback
_dedup_store: dict[str, float] = {}    # key → expiry timestamp
_escalation_spend_today: float = 0.0
_escalation_day: str = ""
_worker_started_at: float = 0.0
_stats: dict = {
    "events_received": 0,
    "events_classified": 0,
    "events_urgent": 0,
    "events_decision_required": 0,
    "events_informational": 0,
    "events_routine": 0,
    "events_deduped": 0,
    "events_signature_failed": 0,
    "escalations": 0,
    "cap_exceeded_events": 0,
}

# ── Config loading ──────────────────────────────────────────────────────

def _config_path() -> Path:
    return Path(__file__).resolve().parent.parent / "config" / "event_monitor.yaml"


def load_config() -> dict:
    """Load event_monitor.yaml config."""
    global _config
    path = _config_path()
    if path.exists():
        with open(path) as fh:
            _config = yaml.safe_load(fh) or {}
    else:
        _config = {}
    logger.info("Event_monitor config loaded from %s", path)
    return _config


def get_config() -> dict:
    if not _config:
        return load_config()
    return _config


# ── ORCA writes (durable, cross-agent) ────────────────────────────

def _write_orca(namespace: str, record: dict) -> dict:
    """Write an event record to the shared ORCA memory layer.

    Primary path: repose.utils.orca.store_artifact() — a real HTTP
    write to the cross-agent ORCA memory-api (host + API key resolved
    via Bitwarden in that module; nothing hardcoded here). This is what makes
    event_monitor's records visible to other agents instead of dying in a
    per-process buffer.

    Fallback path: if ORCA is unreachable (transport error, Bitwarden
    down, config missing), the record is appended to the in-memory
    ``_event_store`` so webhook ingest never blocks. The in-memory list is a
    last-resort buffer ONLY — it is never the primary durability path.
    """
    entry = {
        "namespace": namespace,
        "timestamp": time.time(),
        "timestamp_iso": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        **record,
    }
    try:
        from repose.utils.orca import store_artifact
        store_artifact(
            namespace=namespace,
            content=json.dumps(entry, default=str),
            metadata={
                "source": "event_monitor",
                "type_hint": "episodic",
                "source_id": record.get("event_id"),
                "lane": record.get("lane"),
                "event_source": record.get("source"),
            },
        )
        logger.info("ORCA [%s] wrote event %s", namespace, record.get("event_id", "?"))
    except Exception as exc:
        # ORCA unreachable — degrade to the in-memory buffer so ingest
        # is never lost, but surface the failure loudly. This is the fallback,
        # not the primary path.
        _event_store.append(entry)
        logger.warning(
            "ORCA [%s] write failed for event %s; using in-memory fallback: %s",
            namespace, record.get("event_id", "?"), exc,
        )
    return entry


def get_events(
    namespace: Optional[str] = None,
    lane: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    """Retrieve events from the in-memory store."""
    results = _event_store
    if namespace:
        results = [e for e in results if e.get("namespace") == namespace]
    if lane:
        results = [e for e in results if e.get("lane") == lane]
    return list(reversed(results))[:limit]


def clear_events() -> None:
    _event_store.clear()


# ── Deduplication ───────────────────────────────────────────────────────

def _get_redis():
    """Get a ping-verified Redis connection for the dedup db. Returns None if
    unavailable so dedup degrades to the in-memory store (never blocks ingest).

    Host/port come from Bitwarden (repose-redis-host / repose-redis-port) via the
    shared redis_state helper (RPOSE-008) -- the same resolver Repose uses for all
    coordination state. The previous localhost:6379 / REDIS_HOST env path was
    unreachable in-container, so dedup silently reset to in-memory on every restart
    and duplicate events slipped through.
    """
    try:
        redis_db = get_config().get("dedup", {}).get("redis_db", 3)
        from repose.utils.redis_state import get_redis
        return get_redis(redis_db)
    except Exception:
        return None


def _dedup_check(source: str, dedup_key: str, ttl: int) -> bool:
    """Check if an event is a duplicate. Returns True if DUPLICATE."""
    redis_conn = _get_redis()
    if redis_conn:
        # Redis-backed dedup
        existing = redis_conn.get(dedup_key)
        if existing:
            return True
        redis_conn.setex(dedup_key, ttl, "1")
        return False

    # In-memory fallback
    now = time.time()
    # Purge expired
    expired = [k for k, exp in _dedup_store.items() if exp < now]
    for k in expired:
        del _dedup_store[k]
    if dedup_key in _dedup_store:
        return True
    _dedup_store[dedup_key] = now + ttl
    return False


def _clear_dedup() -> None:
    """Clear dedup store (for testing)."""
    _dedup_store.clear()
    redis_conn = _get_redis()
    if redis_conn:
        try:
            redis_conn.flushdb()
        except Exception:
            pass


# ── Signature Verification ──────────────────────────────────────────────

def verify_stripe_signature(payload: bytes, signature_header: str, secret: str) -> bool:
    """Verify Stripe webhook signature (HMAC-SHA256)."""
    if not secret:
        logger.warning("Stripe signing secret not configured — verification skipped")
        return False
    if not signature_header:
        return False
    try:
        # Stripe signature format: t=timestamp,v1=signature
        parts = {}
        for part in signature_header.split(","):
            k, v = part.split("=", 1)
            parts[k] = v
        timestamp = parts.get("t", "")
        expected_sig = parts.get("v1", "")
        signed_payload = f"{timestamp}.{payload.decode('utf-8')}".encode()
        computed = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(computed, expected_sig)
    except Exception as exc:
        logger.error("Stripe signature verification error: %s", exc)
        return False


def verify_github_signature(payload: bytes, signature_header: str, secret: str) -> bool:
    """Verify GitHub webhook signature (HMAC-SHA256)."""
    if not secret:
        logger.warning("GitHub signing secret not configured — verification skipped")
        return False
    if not signature_header:
        return False
    try:
        # GitHub format: sha256=hexdigest
        expected = signature_header.replace("sha256=", "")
        computed = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(computed, expected)
    except Exception as exc:
        logger.error("GitHub signature verification error: %s", exc)
        return False


def verify_form_signature(payload: bytes, signature_header: str, secret: str) -> bool:
    """Verify form webhook signature (shared secret in header)."""
    if not secret:
        logger.warning("Form signing secret not configured — verification skipped")
        return False
    if not signature_header:
        return False
    try:
        computed = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        expected = signature_header.replace("sha256=", "")
        return hmac.compare_digest(computed, expected)
    except Exception as exc:
        logger.error("Form signature verification error: %s", exc)
        return False


# ── Secret resolution ───────────────────────────────────────────────────

def _resolve_secret(secret_id: str) -> str:
    """Resolve a secret from Bitwarden Secrets Manager.

    Bitwarden SM is the ONLY secrets layer (RPOSE-008): there is no
    environment-variable fallback. If the secret_id is empty the source simply
    has no secret configured (returns ""); if Bitwarden is unreachable or the
    secret is missing, the underlying BitwardenError propagates so the caller
    fails closed rather than silently using an insecure default.
    """
    if not secret_id:
        return ""
    secret_id = secret_id.replace("bitwarden:", "")
    from repose.utils.bitwarden import get_secret
    return get_secret(secret_id)


# ── Payload Sanitization ────────────────────────────────────────────────

SENSITIVE_FIELDS = {
    "client_secret", "secret", "token", "password", "api_key",
    "private_key", "access_token", "refresh_token",
    "card.number", "card.cvc",
}


def sanitize_payload(payload: dict, max_summary_chars: int = 300) -> str:
    """Sanitize a webhook payload for LLM processing. Returns a summary string."""
    def _redact(obj, path=""):
        if isinstance(obj, dict):
            result = {}
            for k, v in obj.items():
                full_path = f"{path}.{k}" if path else k
                if k in SENSITIVE_FIELDS or any(
                    full_path.endswith(f".{sf}") for sf in SENSITIVE_FIELDS
                ):
                    result[k] = "[REDACTED]"
                else:
                    result[k] = _redact(v, full_path)
            return result
        elif isinstance(obj, list):
            return [_redact(item, f"{path}[{i}]") for i, item in enumerate(obj)]
        return obj

    sanitized = _redact(payload)
    summary = json.dumps(sanitized, default=str)
    if len(summary) > max_summary_chars:
        summary = summary[:max_summary_chars - 3] + "..."
    return summary


# ── LLM Classification ──────────────────────────────────────────────────

CLASSIFIER_PROMPT = """You are classifying an incoming event for an operator's workflow system.
Event source: {source}
Event type: {event_type}
Payload summary: {sanitized_payload_summary}

Classify this event into exactly one lane: urgent | decision_required | informational | routine
Return ONLY a JSON object:
{{"lane": string, "confidence": float, "reasoning": string}}
Lane definitions:
- urgent: requires immediate operator attention (payment failure, security alert, system down)
- decision_required: requires operator decision but not time-critical (new lead, form submission, account change)
- informational: noteworthy but no action needed (successful payment, PR merged, new follower)
- routine: expected system event, no attention needed (health check, scheduled job, ping)"""


def _call_llm(prompt: str, model: str = "haiku", event_type: str = "", payload_str: str = "") -> dict:
    """Call LLM for classification. Returns parsed JSON dict.

    In test mode (EVENT_MONITOR_TEST_MODE or EVENT_MONITOR_SKIP_LLM), uses heuristic directly.
    Otherwise attempts OpenAI-compatible API, falling back to heuristic.
    """
    # In test mode, skip LLM API calls entirely
    if os.environ.get("EVENT_MONITOR_TEST_MODE") == "true" or os.environ.get("EVENT_MONITOR_SKIP_LLM") == "true":
        return _heuristic_classify(prompt, event_type=event_type, payload_str=payload_str)

    # Try LiteLLM / OpenAI-compatible API
    try:
        import urllib.request
        cfg = get_config().get("classification", {})
        # Credential comes from Bitwarden SM only (RPOSE-008); endpoint is
        # operator config. Neither is read from the process environment.
        api_base = cfg.get("api_base", "https://api.openai.com/v1")
        api_key = _resolve_secret(cfg.get("api_key_secret_id", ""))

        if "anthropic" in model.lower() or "claude" in model.lower():
            model_name = "claude-3-5-haiku-20241022" if model == "haiku" else "claude-3-5-sonnet-20241022"
        else:
            model_name = model

        llm_payload = json.dumps({
            "model": model_name,
            "messages": [
                {"role": "system", "content": "You are a classification engine. Return ONLY valid JSON."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.0,
            "max_tokens": 200,
        }).encode()

        req = urllib.request.Request(
            f"{api_base}/chat/completions",
            data=llm_payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        content = data["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        logger.warning("LLM API call failed: %s - using heuristic classification", exc)
        return _heuristic_classify(prompt, event_type=event_type, payload_str=payload_str)

    # Parse JSON from response
    try:
        # Extract JSON object
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0]
        elif "```" in content:
            content = content.split("```")[1].split("```")[0]
        result = json.loads(content)
        return result
    except json.JSONDecodeError:
        logger.warning("LLM returned non-JSON: %s", content[:100])
        return _heuristic_classify(prompt, event_type=event_type, payload_str=payload_str)


def _heuristic_classify(prompt: str, event_type: str = "", payload_str: str = "") -> dict:
    """Fallback heuristic classification when LLM is unavailable.

    Only matches against event_type and payload content, NOT the prompt's
    own lane definitions (which contain words like "failure", "down", etc.)
    """
    # Only classify on actual event content, not the prompt template
    check_text = f"{event_type} {payload_str}".lower()
    if any(w in check_text for w in ["payment_failed", "payment_intent.payment_failed",
                                       "card_declined", "charge.failed", "dispute",
                                       "fraud"]):
        return {"lane": "urgent", "confidence": 0.92, "reasoning": "Heuristic: payment failure or security event detected"}
    if any(w in check_text for w in ["customer.subscription.created", "subscription.created",
                                       "customer.subscription.updated",
                                       "payment_intent.succeeded", "charge.succeeded",
                                       "invoice.paid", "invoice.payment_succeeded"]):
        return {"lane": "informational", "confidence": 0.88, "reasoning": "Heuristic: successful/noteworthy event detected"}
    if any(w in check_text for w in ["pr merged", "pull_request.closed",
                                       "issues.opened", "push", "commit"]):
        return {"lane": "informational", "confidence": 0.88, "reasoning": "Heuristic: GitHub activity detected"}
    if any(w in check_text for w in ["form", "lead", "contact", "submission", "message"]):
        return {"lane": "decision_required", "confidence": 0.85, "reasoning": "Heuristic: form submission or lead detected"}
    if any(w in check_text for w in ["health", "check", "ping", "scheduled", "cron",
                                       "heartbeat", "check_run", "deployment",
                                       "deployment_status"]):
        return {"lane": "routine", "confidence": 0.90, "reasoning": "Heuristic: routine/scheduled event detected"}
    return {"lane": "decision_required", "confidence": 0.45, "reasoning": "Heuristic: unable to classify confidently"}


def _check_escalation_cap() -> bool:
    """Check if escalation cost cap has been exceeded. Returns True if CAP EXCEEDED."""
    global _escalation_spend_today, _escalation_day
    cfg = get_config().get("classification", {}).get("escalation", {})
    daily_cap = cfg.get("daily_cost_cap_usd", 5.0)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _escalation_day != today:
        _escalation_spend_today = 0.0
        _escalation_day = today
    return _escalation_spend_today >= daily_cap


def _estimate_llm_cost(model: str) -> float:
    """Estimate cost per classification call."""
    if model == "haiku":
        return 0.001  # ~$0.001 per classification
    elif model == "sonnet":
        return 0.015  # ~$0.015 per classification
    return 0.001


def classify_event(source: str, event_type: str, payload: dict) -> dict:
    """Classify an event via LLM with escalation logic."""
    global _escalation_spend_today, _stats
    cfg = get_config().get("classification", {})
    primary_model = cfg.get("primary_model", "haiku")
    escalation_model = cfg.get("escalation_model", "sonnet")
    escalation_threshold = cfg.get("escalation_threshold", 0.60)
    uncertain_threshold = cfg.get("uncertain_threshold", 0.50)

    sanitized = sanitize_payload(payload)
    prompt = CLASSIFIER_PROMPT.format(
        source=source,
        event_type=event_type,
        sanitized_payload_summary=sanitized,
    )

    # Primary classification with Haiku
    result = _call_llm(prompt, model=primary_model, event_type=event_type, payload_str=sanitized)
    confidence = result.get("confidence", 0.5)
    lane = result.get("lane", "decision_required")
    reasoning = result.get("reasoning", "")
    model_used = primary_model
    _escalation_spend_today += _estimate_llm_cost(primary_model)

    # Escalation: if confidence < threshold, try Sonnet (with cost cap)
    if confidence < escalation_threshold and not _check_escalation_cap():
        logger.info("Confidence %.2f < %.2f, escalating to %s", confidence, escalation_threshold, escalation_model)
        _escalation_spend_today += _estimate_llm_cost(escalation_model)
        _stats["escalations"] += 1
        escalated = _call_llm(prompt, model=escalation_model, event_type=event_type, payload_str=sanitized)
        esc_confidence = escalated.get("confidence", 0.5)
        if esc_confidence > confidence:
            result = escalated
            confidence = esc_confidence
            lane = result.get("lane", lane)
            reasoning = result.get("reasoning", reasoning)
            model_used = escalation_model

    elif _check_escalation_cap() and confidence < escalation_threshold:
        _stats["cap_exceeded_events"] += 1
        lane = "decision_required"
        reasoning = "Escalation cap reached — classified without LLM"
        model_used = "cap_exceeded"
        confidence = 0.0

    # Uncertain routing
    routing_cfg = get_config().get("routing", {})
    on_uncertain = routing_cfg.get("on_uncertain", {})
    if confidence < uncertain_threshold:
        if on_uncertain.get("action") == "route_to_decision_required":
            lane = "decision_required"
            reasoning = f"Uncertain classification (confidence={confidence:.2f}): {reasoning}"

    return {
        "lane": lane,
        "confidence": confidence,
        "reasoning": reasoning,
        "model": model_used,
    }


# ── Event Processing Pipeline ───────────────────────────────────────────

def _get_source_config(source: str) -> dict:
    """Get source-specific config from event_monitor.yaml."""
    cfg = get_config()
    return cfg.get("sources", {}).get(source, {})


def process_event(
    source: str,
    event_type: str = None,
    payload: bytes | dict = None,
    headers: dict = None,
    bypass_signature: bool = False,
    raw_body: bytes | None = None,
) -> dict:
    """Full event processing pipeline. Returns the event_monitor_event record.

    Args:
        source: Event source (stripe, github, form)
        event_type: Event type string. If None, extracted from payload.
        payload: Raw payload bytes or parsed dict.
        headers: HTTP headers dict.
        bypass_signature: If True, skip signature verification (for testing).
        raw_body: The EXACT bytes received on the wire, captured before any
            json.loads. HMAC signature verification MUST run over these bytes —
            re-serializing the parsed dict (json.dumps) produces different bytes
            (key order, whitespace, unicode escaping) and makes legitimate
            Stripe/GitHub signatures fail. When omitted (programmatic callers
            that only have a dict), we reconstruct a best-effort body, but the
            HTTP handler always passes the real wire bytes.
    """
    if payload is None:
        payload = {}
    if headers is None:
        headers = {}

    # Resolve the exact bytes the signature must cover. Prefer the wire bytes;
    # fall back to the payload itself (bytes) or a reserialized dict only for
    # in-process callers that never had the original request body.
    if raw_body is None:
        if isinstance(payload, bytes):
            raw_body = payload
        elif payload:
            raw_body = json.dumps(payload).encode()
        else:
            raw_body = b""

    global _stats
    _stats["events_received"] += 1

    cfg = get_config()
    src_cfg = _get_source_config(source)

    # Extract event_type from payload if not provided
    if event_type is None:
        if isinstance(payload, dict):
            event_type = payload.get("type", payload.get("event_type", "unknown"))
        else:
            event_type = "unknown"

    # ── 1. Signature verification ───────────────────────────────────────
    signing_secret_id = src_cfg.get("signing_secret_id", "")
    signing_secret = _resolve_secret(signing_secret_id)

    if bypass_signature:
        sig_ok = True
    else:
        # HMAC is computed over raw_body — the exact bytes from the wire — for
        # every source. Never over a reserialized dict.
        if source == "stripe":
            sig_header = headers.get("stripe-signature", "")
            sig_ok = verify_stripe_signature(raw_body, sig_header, signing_secret)
        elif source == "github":
            sig_header = headers.get("x-hub-signature-256", "")
            sig_ok = verify_github_signature(raw_body, sig_header, signing_secret)
        elif source == "form":
            sig_header = headers.get("x-form-signature", "")
            sig_ok = verify_form_signature(raw_body, sig_header, signing_secret)
        else:
            sig_ok = False

        # NO environment-variable signature bypass. The previous
        # EVENT_MONITOR_TEST_SKIP_SIGNATURE / EVENT_MONITOR_TEST_MODE env flags
        # could silently disable signature verification in production if the
        # variable leaked into the environment. Tests that need to exercise the
        # post-signature pipeline pass the in-code `bypass_signature=True`
        # argument explicitly — a deliberate call-site flag, never ambient env.

    if not sig_ok:
        _stats["events_signature_failed"] += 1
        _write_orca(
            cfg.get("chronogram", {}).get("system_events_namespace", "system-events"),
            {
                "event_id": str(uuid.uuid4()),
                "source": source,
                "source_event_type": event_type,
                "error": "signature_verification_failed",
                "received_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return {
            "status": "rejected",
            "reason": "signature_verification_failed",
            "discarded": True,
            "discard_reason": "Signature verification failed",
        }

    # ── 2. Deduplication ────────────────────────────────────────────────
    payload_dict = payload if isinstance(payload, dict) else json.loads(payload)

    dedup_strategy = src_cfg.get("dedup_strategy", "source_id")
    dedup_ttl = src_cfg.get("dedup_ttl_seconds", 86400)

    if dedup_strategy == "source_id":
        # For Stripe/GitHub: use webhook delivery ID
        delivery_id = payload_dict.get("id", "")
        if not delivery_id:
            delivery_id = headers.get("x-github-delivery", str(uuid.uuid4()))
        dedup_key = f"event_monitor:{source}:{delivery_id}"
    elif dedup_strategy == "payload_hash":
        # For Form: hash specific fields
        hash_fields = src_cfg.get("payload_hash_fields", ["email", "name", "endpoint"])
        hash_parts = []
        for field in hash_fields:
            val = payload_dict.get(field, "")
            hash_parts.append(str(val))
        hash_input = "|".join(hash_parts)
        dedup_key = f"event_monitor:{source}:hash:{hashlib.sha256(hash_input.encode()).hexdigest()}"
    else:
        dedup_key = f"event_monitor:{source}:{str(uuid.uuid4())}"

    if _dedup_check(source, dedup_key, dedup_ttl):
        _stats["events_deduped"] += 1
        return {"status": "rejected", "reason": "duplicate"}

    # ── 3. Classification ──────────────────────────────────────────────
    received_at = datetime.now(timezone.utc)
    classification = classify_event(source, event_type, payload_dict)
    _stats["events_classified"] += 1

    lane = classification["lane"]
    if lane == "urgent":
        _stats["events_urgent"] += 1
    elif lane == "decision_required":
        _stats["events_decision_required"] += 1
    elif lane == "informational":
        _stats["events_informational"] += 1
    else:
        _stats["events_routine"] += 1

    classified_at = datetime.now(timezone.utc)

    # ── 4. Build canonical event record ─────────────────────────────────
    event_id = str(uuid.uuid4())
    record = {
        "event_id": event_id,
        "schema_version": "1.0",
        "source": source,
        "source_event_type": event_type,
        "received_at": received_at.isoformat(),
        "classified_at": classified_at.isoformat(),
        "lane": lane,
        "classifier_confidence": classification["confidence"],
        "classifier_model": classification["model"],
        "classifier_reasoning": classification["reasoning"],
        "payload_summary": sanitize_payload(payload_dict),
        "surfaced_to_telegram": False,
        "surfaced_at": None,
        "dedup_key": dedup_key,
        "signature_verified": True,
    }

    # ── 5. Route to ORCA namespace ────────────────────────────────
    chronogram_cfg = cfg.get("chronogram", {})
    if lane in ("urgent", "informational", "routine"):
        namespace = chronogram_cfg.get("events_namespace", "event_monitor-events")
    else:
        namespace = chronogram_cfg.get("decision_namespace", "decision-queue")
    # NOTE (RPOSE-FIND7): the ORCA write is deferred until AFTER the
    # surfacing attempt below, so the persisted record reflects the true
    # delivery state (surfaced_to_telegram / surfaced_at). Writing here — before
    # the Telegram send — would durably record surfaced_to_telegram=False even
    # when delivery later succeeded, mismatching the returned record.

    # ── 6. Telegram surfacing ──────────────────────────────────────────
    telegram_cfg = cfg.get("telegram", {})
    should_surface = False
    priority = None

    if lane == "urgent":
        should_surface = True
        priority = telegram_cfg.get("urgent_priority", "critical")
    elif lane == "decision_required":
        should_surface = True
        priority = telegram_cfg.get("decision_required_priority", "informational")
    elif lane == "informational":
        priority = telegram_cfg.get("informational_priority")
        should_surface = priority is not None
    else:
        # routine: never surface
        should_surface = False

    # Uncertain events: do NOT surface to Telegram per SIG-4
    routing_cfg = get_config().get("routing", {})
    if classification["confidence"] < routing_cfg.get("uncertain_threshold", 0.50):
        should_surface = False

    if should_surface and priority:
        try:
            from repose.utils.telegram_router import route_message
            source_emojis = {"stripe": "💳", "github": "🐙", "form": "📝"}
            emoji = source_emojis.get(source, "📡")
            message = (
                f"<b>EVENT_MONITOR · {emoji} {source.upper()} · {event_type}</b>\n"
                f"{classification.get('reasoning', 'No reasoning available')[:200]}\n"
                f"Suggested handling: {classification.get('reasoning', 'Review event')[:100]}"
            )
            result = route_message(agent="event_monitor", message=message, priority=priority)
            if result.get("sent"):
                record["surfaced_to_telegram"] = True
                record["surfaced_at"] = datetime.now(timezone.utc).isoformat()
        except Exception as exc:
            logger.warning("Telegram surfacing failed (non-blocking): %s", exc)

    record["status"] = "processed"
    # Persist now that surfaced_to_telegram / surfaced_at reflect the real
    # outcome of the send attempt (RPOSE-FIND7). Surfacing is wrapped in a
    # non-blocking try/except above, so this write is always reached and the
    # event is never lost.
    _write_orca(namespace, record)
    return record


# ── HTTP Webhook Server ─────────────────────────────────────────────────

class Event_monitorHTTPHandler(BaseHTTPRequestHandler):
    """HTTP request handler for Event_monitor webhook endpoints."""

    def log_message(self, format, *args):
        logger.info("Event_monitor HTTP: %s", format % args)

    def _send_json(self, status: int, data: dict):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length)

    def _get_source_from_path(self, path: str) -> Optional[str]:
        """Extract source name from webhook path."""
        if path.startswith("/webhooks/"):
            return path.split("/")[2]
        return None

    def do_GET(self):
        if self.path == "/health":
            uptime = time.time() - _worker_started_at if _worker_started_at else 0
            self._send_json(200, {
                "status": "healthy",
                "agent": "event_monitor",
                "version": "3.0",
                "uptime_seconds": round(uptime, 1),
                "stats": _stats,
            })
        else:
            self._send_json(404, {"error": "not_found"})

    def do_POST(self):
        source = self._get_source_from_path(self.path)
        if not source:
            self._send_json(404, {"error": "not_found", "path": self.path})
            return

        cfg = get_config()
        src_cfg = cfg.get("sources", {}).get(source)
        if not src_cfg:
            self._send_json(404, {"error": f"unknown_source: {source}"})
            return

        if not src_cfg.get("enabled", False) and source != "form":
            self._send_json(403, {"error": f"source_disabled: {source}"})
            return

        # Capture the EXACT wire bytes before parsing. These are what the HMAC
        # signature must cover; the parsed dict is only for routing downstream.
        body = self._read_body()
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid_json"})
            return

        headers = {k.lower(): v for k, v in self.headers.items()}
        event_type = payload.get("type", payload.get("event_type", "unknown"))

        result = process_event(source, event_type, payload, headers, raw_body=body)

        if result.get("status") == "rejected":
            status_code = 400 if result.get("reason") == "duplicate" else 403
            self._send_json(status_code, result)
        else:
            self._send_json(200, result)


def start_server(port: int = 8080) -> HTTPServer:
    """Start the Event_monitor webhook HTTP server."""
    global _server, _worker_started_at
    server = HTTPServer(("0.0.0.0", port), Event_monitorHTTPHandler)
    _server = server
    _worker_started_at = time.time()
    logger.info("Event_monitor webhook server listening on port %d", port)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def stop_server():
    global _server
    if _server:
        _server.shutdown()
        _server = None


def get_server() -> Optional[HTTPServer]:
    return _server


def get_stats() -> dict:
    uptime = time.time() - _worker_started_at if _worker_started_at else 0
    return {
        "agent": "event_monitor",
        "version": "3.0",
        "uptime_seconds": round(uptime, 1),
        "healthy": _server is not None,
        **{k: v for k, v in _stats.items()},
        "escalation_spend_today": round(_escalation_spend_today, 4),
        "escalation_cap_exceeded": _check_escalation_cap(),
    }


# ── Source Setup Helpers ────────────────────────────────────────────────

def setup_stripe(secret: str) -> dict:
    """Set up Stripe source with signing secret."""
    cfg = get_config()
    secret_id = "repose-stripe-signing-secret"
    from repose.utils.bitwarden import store_secret
    store_secret(secret_id, secret)
    # Enable stripe in config
    cfg["sources"]["stripe"]["enabled"] = True
    cfg["sources"]["stripe"]["signing_secret_id"] = f"bitwarden:{secret_id}"
    _save_config(cfg)
    return {"status": "configured", "source": "stripe"}


def setup_github(secret: str) -> dict:
    """Set up GitHub source with webhook secret."""
    cfg = get_config()
    secret_id = "repose-github-webhook-secret"
    from repose.utils.bitwarden import store_secret
    store_secret(secret_id, secret)
    cfg["sources"]["github"]["enabled"] = True
    cfg["sources"]["github"]["signing_secret_id"] = f"bitwarden:{secret_id}"
    _save_config(cfg)
    return {"status": "configured", "source": "github"}


def _save_config(cfg: dict) -> None:
    """Save event_monitor config back to YAML (with git backup)."""
    path = _config_path()
    # git backup before write
    try:
        import subprocess
        subprocess.run(["git", "add", str(path)], cwd=path.parent.parent,
                       capture_output=True, timeout=5)
    except Exception:
        pass
    with open(path, "w") as fh:
        yaml.dump(cfg, fh, default_flow_style=False, sort_keys=False)
    logger.info("Config saved to %s", path)
    global _config
    _config = cfg


def reset_stats() -> None:
    """Reset all stats (for testing)."""
    global _stats, _escalation_spend_today, _escalation_day
    for k in _stats:
        _stats[k] = 0
    _escalation_spend_today = 0.0
    _escalation_day = ""


# ── Missing functions needed by event_monitor_cli ────────────────────────────────

def verify_signature(source: str, payload: bytes, headers: dict) -> bool:
    """Unified signature verification for any source.

    Used by the CLI setup wizards to test signature verification.
    Returns True if signature is valid.
    """
    cfg = get_config()
    src_cfg = cfg.get("sources", {}).get(source, {})
    signing_secret_id = src_cfg.get("signing_secret_id", "")
    signing_secret = _resolve_secret(signing_secret_id)

    if not signing_secret:
        # No configured secret means the signature cannot be verified — fail
        # closed. There is deliberately NO env-based "accept any signature"
        # path: a leaked EVENT_MONITOR_TEST_MODE could otherwise silently
        # disable webhook verification in production.
        return False

    if source == "stripe":
        sig_header = headers.get("Stripe-Signature",
                                 headers.get("stripe-signature", ""))
        return verify_stripe_signature(payload, sig_header, signing_secret)
    elif source == "github":
        sig_header = headers.get("X-Hub-Signature-256",
                                 headers.get("x-hub-signature-256", ""))
        return verify_github_signature(payload, sig_header, signing_secret)
    elif source == "form":
        sig_header = headers.get("X-Form-Signature",
                                 headers.get("x-form-signature", ""))
        return verify_form_signature(payload, sig_header, signing_secret)
    return False


def set_escalation_usage(spend: float) -> None:
    """Set escalation spend for testing cap enforcement."""
    global _escalation_spend_today, _escalation_day
    _escalation_spend_today = spend
    _escalation_day = datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load_event_monitor_config() -> dict:
    """Load event_monitor config with test mode support (environment overrides)."""
    cfg = load_config()
    # Apply test mode overrides from env vars
    if os.environ.get("EVENT_MONITOR_TEST_MODE") == "true":
        # Ensure stripe is enabled for testing
        if "sources" in cfg and "stripe" in cfg["sources"]:
            cfg["sources"]["stripe"]["enabled"] = True
        if "sources" in cfg and "github" in cfg["sources"]:
            cfg["sources"]["github"]["enabled"] = True
    return cfg


def reload_event_monitor_config() -> dict:
    """Force reload event_monitor config from disk."""
    global _config
    _config = {}
    return _load_event_monitor_config()


def list_events(
    lane: str | None = None,
    source: str | None = None,
    last_hours: float | None = None,
) -> list[dict]:
    """List processed events with optional filters.

    Args:
        lane: Filter by classification lane (urgent, decision_required, informational, routine)
        source: Filter by event source (stripe, github, form)
        last_hours: Only return events from the last N hours

    Returns:
        List of event records, most recent first.
    """
    results = _event_store

    if lane:
        results = [e for e in results if e.get("lane") == lane]
    if source:
        results = [e for e in results if e.get("source") == source]
    if last_hours is not None:
        cutoff = time.time() - (last_hours * 3600)
        results = [e for e in results if e.get("timestamp", 0) >= cutoff]

    # Sort by timestamp descending (most recent first)
    results = sorted(results, key=lambda e: e.get("timestamp", 0), reverse=True)
    return results


def get_status() -> dict:
    """Get Event_monitor worker status (alias for get_stats with health info)."""
    stats = get_stats()
    return stats


def reset_state() -> None:
    """Reset all Event_monitor state (stats, events, dedup) for testing."""
    reset_stats()
    clear_events()
    _clear_dedup()
    global _escalation_spend_today, _escalation_day
    _escalation_spend_today = 0.0
    _escalation_day = ""


# ── Entry point (Repose wiring) ─────────────────────────────────────────
def main() -> int:
    """Run the Event_monitor webhook server as a long-lived process.

    Loads event_monitor.yaml, starts the HTTP server on the configured port, and
    blocks until SIGTERM/SIGINT so the daemon serve thread keeps running
    under systemd. This is the missing entry point — no pipeline logic
    changes, it only wires start_server() to a process lifecycle.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # Single-instance guard: refuse to start a second webhook server if a live
    # one is already running (orphaned across a docker-exec systemd restart).
    if not DaemonGuard("event_monitor").acquire():
        return 1

    cfg = load_config()
    port = int(cfg.get("webhook_ingress", {}).get("listen_port", 8080))

    stop = threading.Event()

    def _handle_signal(signum, _frame):
        logger.info("Event_monitor received signal %s - shutting down", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    start_server(port=port)
    logger.info("Event_monitor webhook server up on port %d - waiting for events", port)
    stop.wait()
    stop_server()
    logger.info("Event_monitor webhook server stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
