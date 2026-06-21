"""
Shared Telegram routing utility for Repose OS.

All four agents (Morning_brief, Intel_feed, Event_monitor, Observer) import this.
No agent maintains its own telegram.py wrapper after this.

Priority routing model:
  - repose-critical: Event_monitor urgent events, Observer critical observations, Morning_brief delivery failures
  - repose-informational: Everything else

Rate limits are enforced per-agent. Critical priority bypasses rate limits.
Secrets resolved via Bitwarden SDK at import time.
"""

import json
import logging
import time
import urllib.request
import urllib.error

from repose.utils.bitwarden import get_secret
from repose.utils.orca import log_system_event

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-agent rate limits (LOCKED — do not deviate)
# ---------------------------------------------------------------------------
AGENT_RATE_LIMITS = {
    "morning_brief":  {"per_minute": 2,  "per_hour": 10},
    "intel_feed":   {"per_minute": 5,  "per_hour": 15},
    "event_monitor":   {"per_minute": 3,  "per_hour": 60},
    "observer":  {"per_minute": 6,  "per_hour": 20},
}

VALID_AGENTS = set(AGENT_RATE_LIMITS.keys())
VALID_PRIORITIES = {"critical", "informational"}

# ---------------------------------------------------------------------------
# Redis-backed rate limiting (RPOSE-010)
# ---------------------------------------------------------------------------
# Rate-limit counters live in Redis so all workers share one count. An in-memory
# bucket per worker undercounts and can amplify ORCA writes during alert
# storms. Redis unreachable => raise (fail closed).


def _rate_redis():
    # Rate-limit counters live on their own Redis db (config: rate_limit.redis_db,
    # default 4) so they stay isolated from ORCA's db. Reading it from
    # config — rather than hardcoding db 0 — keeps the limiter pointed at the
    # same db the rest of the system reserves for rate limiting (RPOSE-FIND9).
    from repose.utils.redis_state import get_redis
    from repose.config import repose_config
    db = repose_config.get("rate_limit", {}).get("redis_db", 4)
    return get_redis(db)

# ---------------------------------------------------------------------------
# Telegram credentials (loaded lazily)
# ---------------------------------------------------------------------------
_credentials: dict = {}
_credentials_loaded: bool = False


def _load_credentials():
    """Load Telegram bot token and channel IDs from Bitwarden via config."""
    global _credentials, _credentials_loaded
    if _credentials_loaded:
        return

    try:
        from repose.config import repose_config

        tg_cfg = repose_config.get("telegram", {})
        bot_token_id = tg_cfg.get("bot_token_secret_id", "")
        crit_id = tg_cfg.get("channels", {}).get("critical", "")
        info_id = tg_cfg.get("channels", {}).get("informational", "")

        # Strip "bitwarden:" prefix
        bot_token_id = bot_token_id.replace("bitwarden:", "")
        crit_id = crit_id.replace("bitwarden:", "")
        info_id = info_id.replace("bitwarden:", "")

        # If config has no telegram settings, try well-known secret IDs directly
        if not bot_token_id and not crit_id and not info_id:
            bot_token_id = "repose-telegram-bot-token"
            crit_id = "repose-telegram-critical-channel-id"
            info_id = "repose-telegram-informational-channel-id"

        _credentials = {
            "bot_token": get_secret(bot_token_id),
            "channels": {
                "critical": get_secret(crit_id),
                "informational": get_secret(info_id),
            },
        }
        _credentials_loaded = True
        logger.info("Telegram credentials loaded")
    except Exception as exc:
        logger.error("Failed to load Telegram credentials from config: %s", exc)
        # Fallback: try well-known Bitwarden secret IDs directly (env vars or SDK)
        try:
            _credentials = {
                "bot_token": get_secret("repose-telegram-bot-token"),
                "channels": {
                    "critical": get_secret("repose-telegram-critical-channel-id"),
                    "informational": get_secret("repose-telegram-informational-channel-id"),
                },
            }
            logger.info("Telegram credentials loaded via fallback")
        except Exception as fb_exc:
            logger.error("Fallback credential load also failed: %s", fb_exc)
            _credentials = {
                "bot_token": "",
                "channels": {"critical": "", "informational": ""},
            }
        _credentials_loaded = True


def reset_credentials():
    """Reset credential cache (for testing)."""
    global _credentials, _credentials_loaded
    _credentials = {}
    _credentials_loaded = False


def _set_telegram_config_override(config: dict) -> None:
    """Override Telegram config for testing (bypass Bitwarden).

    Sets credentials directly so tests can run without real Bitwarden SDK.
    """
    global _credentials, _credentials_loaded
    _credentials = config
    _credentials_loaded = True


def _clear_telegram_config_override() -> None:
    """Clear Telegram config override (for testing)."""
    global _credentials, _credentials_loaded
    _credentials = {}
    _credentials_loaded = False


def _check_rate_limit(agent: str) -> tuple[bool, str]:
    """Check if agent is within its rate limits.

    Returns (allowed, reason).
    """
    limits = AGENT_RATE_LIMITS.get(agent)
    if not limits:
        return True, ""

    r = _rate_redis()
    now = time.time()
    window_minute = int(now // 60)
    window_hour = int(now // 3600)
    min_key = f"repose:telegram:rate:{agent}:{window_minute}"
    hour_key = f"repose:telegram:rate:{agent}:h{window_hour}"

    # INCR the fixed-window counters; set the TTL on first increment so the
    # window self-expires (RPOSE-010).
    min_count = r.incr(min_key)
    if min_count == 1:
        r.expire(min_key, 120)
    hour_count = r.incr(hour_key)
    if hour_count == 1:
        r.expire(hour_key, 7200)

    if min_count > limits["per_minute"]:
        return False, f"per_minute limit ({limits['per_minute']}) reached"
    if hour_count > limits["per_hour"]:
        return False, f"per_hour limit ({limits['per_hour']}) reached"
    return True, ""


def reset_rate_limits(agent: str | None = None):
    """Reset rate limit counters (for testing)."""
    r = _rate_redis()
    pattern = f"repose:telegram:rate:{agent}:*" if agent else "repose:telegram:rate:*"
    keys = list(r.scan_iter(match=pattern))
    if keys:
        r.delete(*keys)


def _send_telegram_message(
    bot_token: str,
    chat_id: str,
    message: str,
    max_retries: int = 3,
) -> tuple[bool, str]:
    """Send a Telegram message with exponential backoff retry.

    Args:
        bot_token: Telegram bot token.
        chat_id: Target channel/chat ID.
        message: Message text to send.
        max_retries: Maximum retry attempts (default 3).

    Returns:
        (sent, reason) tuple.
    """
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")

    last_error = ""
    for attempt in range(max_retries + 1):
        try:
            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())

            if resp.status == 200 and data.get("ok"):
                return True, ""

            error_desc = data.get("description", f"HTTP {resp.status}")
            last_error = f"Telegram API error: {error_desc}"

            if resp.status in (401, 403, 400):
                logger.error("Non-retryable Telegram error: %s", last_error)
                return False, last_error

        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            last_error = f"Telegram API request failed: {exc}"

        if attempt < max_retries:
            wait = 2 ** attempt  # 1s, 2s, 4s
            logger.warning(
                "Telegram send attempt %d/%d failed: %s. Retrying in %ds...",
                attempt + 1, max_retries + 1, last_error, wait,
            )
            time.sleep(wait)

    logger.error("All %d Telegram send attempts failed: %s", max_retries + 1, last_error)
    return False, f"Telegram API failure: {last_error}"


# ---------------------------------------------------------------------------
# Core routing function
# ---------------------------------------------------------------------------
def route_message(
    agent: str,
    message: str,
    priority: str,
    bypass_rate_limit: bool = False,
) -> dict:
    """Route a message to the appropriate Telegram channel.

    Args:
        agent: Agent name ("morning_brief", "intel_feed", "event_monitor", "observer").
        message: Pre-formatted message body.
        priority: "critical" or "informational".
        bypass_rate_limit: If True, skip rate-limit check.

    Returns:
        {"sent": bool, "channel": str, "rate_limited": bool, "reason": str}
    """
    _load_credentials()

    # Validate agent
    if agent not in VALID_AGENTS:
        return {"sent": False, "channel": "", "rate_limited": False,
                "reason": f"Invalid agent: {agent}"}

    # Validate priority
    if priority not in VALID_PRIORITIES:
        return {"sent": False, "channel": "", "rate_limited": False,
                "reason": f"Invalid priority: {priority}"}

    # Determine channel
    if priority == "critical":
        channel = "critical"
    else:
        channel = "informational"

    # Rate limit check (critical bypasses)
    if priority != "critical" and not bypass_rate_limit:
        allowed, reason = _check_rate_limit(agent)
        if not allowed:
            log_system_event(
                namespace="system-events",
                agent=agent,
                message_preview=message[:100],
                rate_limited=True,
                extra={"priority": priority, "rate_limit_reason": reason},
            )
            return {
                "sent": False,
                "channel": channel,
                "rate_limited": True,
                "reason": f"Rate limited: {reason}",
            }

    # Get credentials
    chat_id = _credentials.get("channels", {}).get(channel, "")
    bot_token = _credentials.get("bot_token", "")

    if not bot_token or not chat_id:
        err = "Telegram not configured"
        logger.warning(err)
        log_system_event(
            namespace="system-events",
            agent=agent,
            message_preview=message[:100],
            error=err,
            extra={"priority": priority, "channel": channel},
        )
        return {"sent": False, "channel": channel, "rate_limited": False,
                "reason": err}

    # Send via Telegram
    raw_result = _send_telegram_message(bot_token, chat_id, message)

    # Handle both tuple (production) and bool (test mock) returns
    if isinstance(raw_result, tuple):
        sent, reason = raw_result
    else:
        sent = bool(raw_result)
        reason = "" if sent else "Telegram API failure"

    if not sent:
        full_reason = f"Telegram API failure: {reason}" if "Telegram API failure" not in reason else reason
        log_system_event(
            namespace="system-events",
            agent=agent,
            message_preview=message[:100],
            error=full_reason,
            extra={"priority": priority, "channel": channel},
        )
        return {
            "sent": False,
            "channel": channel,
            "rate_limited": False,
            "reason": full_reason,
        }

    return {
        "sent": True,
        "channel": channel,
        "rate_limited": False,
        "reason": "",
    }


def route_message_sync(
    agent: str,
    message: str,
    priority: str,
    bypass_rate_limit: bool = False,
) -> dict:
    """Synchronous convenience wrapper for route_message.

    Identical behavior to route_message(). Provided for API consistency.
    """
    return route_message(agent, message, priority, bypass_rate_limit)


def get_config_status() -> dict:
    """Return current Telegram configuration status for diagnostics.

    Returns:
        dict with bot_token_configured, channels, rate_limits keys.
    """
    _load_credentials()
    return {
        "bot_token_configured": bool(_credentials.get("bot_token")),
        "critical_channel_configured": bool(
            _credentials.get("channels", {}).get("critical")
        ),
        "informational_channel_configured": bool(
            _credentials.get("channels", {}).get("informational")
        ),
        "rate_limits": dict(AGENT_RATE_LIMITS),
    }
