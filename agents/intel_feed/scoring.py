"""LLM scoring engine for Intel_feed Lite.

Tiered scoring: Haiku primary, Sonnet escalation on low confidence.
Novelty scoring: embedding similarity against intel_feed-archive.

Routes through the LiteLLM gateway over HTTP (OpenAI-compatible; no SDK),
with a deterministic keyword-heuristic fallback when the gateway or its
Bitwarden-resolved key is unavailable, so the scan path never blocks.
"""

import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from repose.agents.intel_feed.config import get_intel_feed_config

logger = logging.getLogger(__name__)

# In-memory archive cache for novelty scoring
_archive_cache: list[dict] = []


# ---------------------------------------------------------------------------
# LLM Scoring prompt (STATIC — Section 8)
# ---------------------------------------------------------------------------

SCORING_PROMPT = (
    "You are scoring a research signal for relevance to an operator's active work.\n"
    "Active tracks: {active_tracks}\n"
    "Signal title: {sanitized_title}\n"
    "Signal summary: {sanitized_summary}\n"
    "Source: {source_id}\n"
    "\n"
    "Score this signal's relevance on a scale from 0.0 to 1.0.\n"
    "Return ONLY a JSON object: {{\"score\": float, \"confidence\": float, \"reasoning\": string}}"
)


# ---------------------------------------------------------------------------
# Heuristic scoring fallback (MVP container — no LiteLLM)
# ---------------------------------------------------------------------------

def _heuristic_score(
    title: str,
    summary: str,
    active_tracks: list[str],
    keywords: list[str],
) -> dict:
    """Deterministic heuristic scorer for MVP when LiteLLM is unavailable.

    Scores based on keyword overlap with active tracks and keyword list.
    Produces deterministic scores for POL verification.
    """
    combined = f"{title.lower()} {summary.lower()}"
    tracks_text = " ".join(active_tracks).lower()
    kw_text = " ".join(keywords).lower() if keywords else ""

    # Count keyword overlaps
    track_words = set(tracks_text.split())
    item_words = set(combined.split())
    kw_words = set(kw_text.split())

    track_overlap = len(item_words & track_words)
    kw_overlap = len(item_words & kw_words)

    total_track_words = max(len(track_words), 1)
    total_kw_words = max(len(kw_words), 1)

    # Score based on overlap ratios
    track_ratio = min(track_overlap / max(total_track_words * 0.1, 1), 1.0)
    kw_ratio = min(kw_overlap / max(total_kw_words * 0.1, 1), 1.0)

    score = round(0.30 + (track_ratio * 0.35) + (kw_ratio * 0.35), 3)
    score = min(max(score, 0.0), 1.0)

    # Confidence based on amount of content
    content_len = len(combined)
    if content_len > 500:
        confidence = 0.85
    elif content_len > 200:
        confidence = 0.75
    elif content_len > 50:
        confidence = 0.65
    else:
        confidence = 0.50

    reasoning = f"Heuristic: track_overlap={track_overlap}, kw_overlap={kw_overlap}, content_len={content_len}"

    return {"score": score, "confidence": confidence, "reasoning": reasoning}


# ---------------------------------------------------------------------------
# LLM scoring via LiteLLM (production path)
# ---------------------------------------------------------------------------

def _call_litellm(
    prompt: str,
    model: str,
    temperature: float = 0.0,
    max_tokens: int = 512,
) -> Optional[dict]:
    """Score via the LiteLLM gateway (OpenAI-compatible HTTP).

    Endpoint + credential come from intel_feed.yaml gates.llm_relevance: api_base is
    operator config and the API key is resolved through Bitwarden SM only
    (RPOSE-008) -- never from the process environment. The litellm SDK is
    intentionally not used (absent from the hermes venv); this posts directly to
    the gateway, mirroring agents/event_monitor.py::_call_llm.

    Returns parsed JSON ({score, confidence, reasoning}) or None on any failure,
    so the caller falls back to the deterministic heuristic and the live scan
    path is never blocked.
    """
    import urllib.request

    gate = get_intel_feed_config().get("gates", {}).get("llm_relevance", {})
    api_base = gate.get("api_base")
    secret_id = gate.get("api_key_secret_id", "")
    if not api_base or not secret_id:
        return None
    try:
        from repose.utils.bitwarden import get_secret
        api_key = get_secret(secret_id.replace("bitwarden:", ""))
    except Exception as exc:
        logger.warning("LiteLLM key resolve failed (%s); using heuristic", exc)
        return None

    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }).encode()
    req = urllib.request.Request(
        api_base.rstrip("/") + "/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        raw = data["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        logger.warning("LiteLLM gateway call failed (%s); using heuristic", exc)
        return None

    if "```json" in raw:
        raw = raw.split("```json")[1].split("```")[0]
    elif "```" in raw:
        raw = raw.split("```")[1].split("```")[0]
    try:
        return json.loads(raw.strip())
    except json.JSONDecodeError as exc:
        logger.warning("LiteLLM returned non-JSON (%s); using heuristic", exc)
        return None


# ---------------------------------------------------------------------------
# Primary scoring function
# ---------------------------------------------------------------------------

def score_item_llm(
    title: str,
    summary: str,
    source_id: str,
    active_tracks: list[str],
    keywords: list[str] | None = None,
    model: str | None = None,
) -> dict:
    """Score a single item with LLM (Haiku primary, Sonnet escalation).

    Args:
        title: Sanitized title.
        summary: Sanitized summary (max 500 chars).
        source_id: Source identifier.
        active_tracks: Active tracks context strings.
        keywords: Keywords for heuristic fallback.
        model: Override model (default: config primary_model).

    Returns:
        dict: {score, confidence, reasoning, model_used, escalated, escalation_reason}
    """
    config = get_intel_feed_config()
    gate_config = config.get("gates", {}).get("llm_relevance", {})

    primary_model = model or gate_config.get("primary_model", "haiku")
    escalation_model = gate_config.get("escalation_model", "sonnet")
    escalation_threshold = gate_config.get("escalation_threshold", 0.6)

    prompt = SCORING_PROMPT.format(
        active_tracks=", ".join(active_tracks),
        sanitized_title=title,
        sanitized_summary=summary[:500],
        source_id=source_id,
    )

    # Primary scoring
    result = _call_litellm(prompt, primary_model)

    if result is None:
        # LiteLLM unavailable — use heuristic fallback
        from repose.agents.intel_feed.config import get_keywords
        kw = keywords or get_keywords()
        result = _heuristic_score(title, summary, active_tracks, kw)
        result["model_used"] = "heuristic"
        result["escalated"] = False
        result["escalation_reason"] = None
        return result

    result["model_used"] = primary_model
    result["escalated"] = False
    result["escalation_reason"] = None

    # Check if escalation needed
    confidence = result.get("confidence", 0.0)
    if confidence < escalation_threshold:
        logger.info(
            "Escalating to %s: confidence %.3f < %.3f",
            escalation_model, confidence, escalation_threshold,
        )
        escalated_result = _call_litellm(prompt, escalation_model)
        if escalated_result:
            result = escalated_result
            result["model_used"] = escalation_model
            result["escalated"] = True
            result["escalation_reason"] = f"confidence {confidence:.3f} < {escalation_threshold}"

    return result


# ---------------------------------------------------------------------------
# Novelty scoring
# ---------------------------------------------------------------------------

def _load_archive() -> list[dict]:
    """Load existing intel_feed-archive records for novelty comparison."""
    global _archive_cache
    if _archive_cache:
        return _archive_cache

    # In production, this queries Chronogram/Redis for intel_feed-archive namespace.
    # For MVP, the archive is populated during scan runs.
    return _archive_cache


def _add_to_archive_cache(record: dict) -> None:
    """Add a record to the in-memory archive cache."""
    global _archive_cache
    _archive_cache.append(record)


def _compute_text_similarity(text1: str, text2: str) -> float:
    """Compute simple TF-IDF-like cosine similarity between two texts.

    Returns 0.0 (identical) to 1.0 (completely different).
    """
    import math
    from collections import Counter

    def tokenize(text: str) -> list[str]:
        return re.findall(r'\b[a-zA-Z]{3,}\b', text.lower())

    tokens1 = tokenize(text1)
    tokens2 = tokenize(text2)

    if not tokens1 or not tokens2:
        return 1.0  # No tokens to compare = novel

    # Term frequencies
    tf1 = Counter(tokens1)
    tf2 = Counter(tokens2)

    # IDF-like: downweight very common words
    all_terms = set(tf1.keys()) | set(tf2.keys())
    doc_freq = Counter()
    for t in all_terms:
        if t in tf1:
            doc_freq[t] += 1
        if t in tf2:
            doc_freq[t] += 1

    # Simple vector dot product
    dot = 0.0
    mag1 = 0.0
    mag2 = 0.0

    for term in all_terms:
        w1 = tf1.get(term, 0) * (1.0 / doc_freq.get(term, 1))
        w2 = tf2.get(term, 0) * (1.0 / doc_freq.get(term, 1))
        dot += w1 * w2
        mag1 += w1 * w1
        mag2 += w2 * w2

    if mag1 == 0 or mag2 == 0:
        return 1.0

    cosine_sim = dot / (math.sqrt(mag1) * math.sqrt(mag2))

    # Invert: 0.0 = identical, 1.0 = completely novel
    # cosine_sim of 1.0 means identical → novelty = 1.0 - 1.0 = 0.0
    # cosine_sim of 0.0 means completely different → novelty = 1.0 - 0.0 = 1.0
    novelty = round(1.0 - cosine_sim, 4)
    return max(0.0, min(1.0, novelty))


def score_novelty(title: str, summary: str, source_id: str | None = None) -> dict:
    """Compute novelty score against intel_feed-archive.

    In production, uses Voyage-3 embeddings via LiteLLM.
    For MVP, uses TF-IDF cosine similarity against archived items.

    Args:
        title: Sanitized title.
        summary: Sanitized summary.
        source_id: Source identifier.

    Returns:
        dict: {score, model_used, compared_against}
    """
    config = get_intel_feed_config()
    novelty_config = config.get("gates", {}).get("novelty", {})

    archive = _load_archive()

    if not archive:
        # Empty archive = everything is novel
        logger.info("Novelty: empty archive, score=1.0")
        return {"score": 1.0, "model_used": "tfidf-local", "compared_against": 0}

    combined = f"{title} {summary}"

    # Compare against all archived items
    similarities = []
    lookback_days = novelty_config.get("lookback_days", 30)

    for record in archive:
        archived_text = f"{record.get('title', '')} {record.get('summary', '')}"
        sim = _compute_text_similarity(combined, archived_text)
        similarities.append(sim)

    if not similarities:
        return {"score": 1.0, "model_used": "tfidf-local", "compared_against": 0}

    # Take the minimum similarity = most similar archived item
    # Higher similarity to archive = lower novelty
    min_similarity = min(similarities)
    avg_similarity = sum(similarities) / len(similarities)

    # Blend: 70% min similarity, 30% average
    novelty = round(0.7 * min_similarity + 0.3 * avg_similarity, 4)

    logger.info(
        "Novelty: score=%.3f (min=%.3f, avg=%.3f) against %d records",
        novelty, min_similarity, avg_similarity, len(similarities),
    )
    return {
        "score": novelty,
        "model_used": "tfidf-local",
        "compared_against": len(similarities),
    }


# ---------------------------------------------------------------------------
# Cold-start warmup
# ---------------------------------------------------------------------------

def is_warmup_active() -> dict:
    """Check if cold-start warmup is active.

    Returns:
        dict: {active, days_remaining, warmup_days, warmup_start_iso}
    """
    config = get_intel_feed_config()
    cold_start = config.get("cold_start", {})

    enabled = cold_start.get("enabled", True)
    warmup_days = cold_start.get("warmup_days", 14)

    if not enabled:
        return {"active": False, "days_remaining": 0, "warmup_days": warmup_days, "warmup_start_iso": None}

    # Check if warmup period has elapsed based on first scan timestamp
    from pathlib import Path
    import json

    warmup_file = Path(__file__).resolve().parent.parent.parent.parent / ".intel_feed_warmup.json"
    now_iso = datetime.now(timezone.utc).isoformat()

    if not warmup_file.exists():
        # First run — start warmup
        warmup_data = {
            "started_at": now_iso,
            "warmup_days": warmup_days,
        }
        warmup_file.write_text(json.dumps(warmup_data, indent=2))
        return {
            "active": True,
            "days_remaining": warmup_days,
            "warmup_days": warmup_days,
            "warmup_start_iso": now_iso,
        }

    try:
        warmup_data = json.loads(warmup_file.read_text())
        started_raw = warmup_data["started_at"]
        # Handle both float (epoch) and string (ISO) formats
        if isinstance(started_raw, (int, float)):
            started_at = datetime.fromtimestamp(started_raw, tz=timezone.utc)
        else:
            started_at = datetime.fromisoformat(str(started_raw))
        elapsed = (datetime.now(timezone.utc) - started_at).total_seconds() / 86400
        remaining = max(0, warmup_days - elapsed)

        if remaining <= 0:
            return {
                "active": False,
                "days_remaining": 0,
                "warmup_days": warmup_days,
                "warmup_start_iso": warmup_data["started_at"],
            }

        return {
            "active": True,
            "days_remaining": round(remaining, 1),
            "warmup_days": warmup_days,
            "warmup_start_iso": warmup_data["started_at"],
        }
    except (json.JSONDecodeError, KeyError, ValueError):
        warmup_file.unlink(missing_ok=True)
        return {"active": False, "days_remaining": 0, "warmup_days": warmup_days, "warmup_start_iso": None}


def get_warmup_max_surfaces() -> int:
    """Get max surfaces per scan during warmup."""
    config = get_intel_feed_config()
    return config.get("cold_start", {}).get("warmup_max_surfaces_per_scan", 3)


def reset_warmup():
    """Reset warmup state (for testing)."""
    from pathlib import Path
    warmup_file = Path(__file__).resolve().parent.parent.parent.parent / ".intel_feed_warmup.json"
    warmup_file.unlink(missing_ok=True)
