"""Significance gates for Intel_feed Lite.

Three gates that a signal item must clear to be surfaced:
1. Keyword relevance (pre-LLM, cheap filter)
2. LLM relevance score
3. Novelty score (embedding similarity)

Uses scoring.py which already implements score_item_llm() and score_novelty().
"""

import logging
from typing import Optional

from repose.agents.intel_feed.config import get_intel_feed_config, get_keywords
from repose.agents.intel_feed.scoring import score_item_llm, score_novelty

logger = logging.getLogger(__name__)


def check_keyword_gate(title: str, summary: str) -> dict:
    """Gate 1: Check if item has keyword matches.

    Returns:
        {"passed": bool, "match_count": int, "matched_keywords": list[str]}
    """
    config = get_intel_feed_config()
    gate_config = config.get("gates", {}).get("keyword_relevance", {})

    if not gate_config.get("enabled", True):
        return {"passed": True, "match_count": 0, "matched_keywords": [], "skipped": True}

    min_matches = gate_config.get("min_keyword_matches", 1)
    keywords = get_keywords()

    text = f"{title} {summary}".lower()
    matched = [kw for kw in keywords if kw.lower() in text]

    passed = len(matched) >= min_matches
    return {
        "passed": passed,
        "match_count": len(matched),
        "matched_keywords": matched,
    }


def check_llm_gate(
    title: str,
    summary: str,
    source_id: str,
    active_tracks: list[str],
) -> dict:
    """Gate 2: LLM relevance scoring.

    Returns:
        {"passed": bool, "score": float, "confidence": float,
         "reasoning": str, "model_used": str, "escalated": bool}
    """
    config = get_intel_feed_config()
    gate_config = config.get("gates", {}).get("llm_relevance", {})

    if not gate_config.get("enabled", True):
        return {"passed": True, "score": 0.0, "confidence": 1.0,
                "reasoning": "LLM gate disabled", "model_used": "none", "escalated": False}

    threshold = gate_config.get("threshold", 0.65)
    result = score_item_llm(title, summary, source_id, active_tracks)
    passed = result["score"] >= threshold

    return {
        "passed": passed,
        "score": result["score"],
        "confidence": result.get("confidence", 0.7),
        "reasoning": result.get("reasoning", ""),
        "model_used": result.get("model_used", "unknown"),
        "escalated": result.get("escalated", False),
        "threshold": threshold,
    }


def check_novelty_gate(title: str, summary: str) -> dict:
    """Gate 3: Novelty scoring.

    Returns:
        {"passed": bool, "score": float}
    """
    config = get_intel_feed_config()
    gate_config = config.get("gates", {}).get("novelty", {})

    if not gate_config.get("enabled", True):
        return {"passed": True, "score": 1.0, "skipped": True}

    threshold = gate_config.get("threshold", 0.30)
    result = score_novelty(title, summary)
    novelty_score = result["score"]
    passed = novelty_score >= threshold

    return {
        "passed": passed,
        "score": novelty_score,
        "threshold": threshold,
    }


def evaluate_all_gates(
    title: str = None,
    summary: str = None,
    source_id: str = None,
    active_tracks: list[str] = None,
    warmup_mode: bool = False,
    kw_result: dict = None,
    llm_gate: dict = None,
    nov_gate: dict = None,
) -> dict:
    """Evaluate all three significance gates on a signal item.

    Two calling conventions:
    1. evaluate_all_gates(kw_result, llm_gate, nov_gate)
       - Pass three already-computed gate results as positional args (title/summary/etc as None).
    2. evaluate_all_gates(title, summary, source_id, active_tracks, warmup_mode)
       - Compute all gates from scratch.

    Returns:
        Full gate evaluation dict with all_passed key.
    """
    # Determine which calling convention was used
    if kw_result is not None and llm_gate is not None and nov_gate is not None:
        # Convention 2: combine pre-computed gate results
        kw_passed = kw_result.get("passed", False)
        llm_passed = llm_gate.get("passed", False)
        nov_passed = nov_gate.get("passed", False)

        return {
            "keyword": {"passed": kw_passed, "result": kw_result},
            "llm": {"passed": llm_passed, "result": llm_gate},
            "novelty": {"passed": nov_passed, "result": nov_gate},
            "all_passed": kw_passed and llm_passed and nov_passed,
        }
    elif title is not None and summary is not None and source_id is not None:
        # Convention 1: compute gates from scratch
        keyword = check_keyword_gate(title, summary)
        if not keyword["passed"]:
            logger.info(
                "Item failed keyword gate (matches: %d), skipping LLM",
                keyword["match_count"],
            )
            return {
                "gate_keyword": keyword["passed"],
                "gate_keyword_details": keyword,
                "gate_llm_score": 0.0,
                "gate_llm_details": None,
                "gate_novelty_score": 0.0,
                "gate_novelty_details": None,
                "all_gates_passed": False,
            }

        llm = check_llm_gate(title, summary, source_id, active_tracks)

        # During warmup, use elevated threshold
        if warmup_mode:
            config = get_intel_feed_config()
            warmup_threshold = config.get("cold_start", {}).get("warmup_surface_threshold", 0.80)
            llm["passed"] = llm["score"] >= warmup_threshold

        if not llm["passed"]:
            logger.info("Item failed LLM gate (score: %.3f)", llm["score"])
            return {
                "gate_keyword": keyword["passed"],
                "gate_keyword_details": keyword,
                "gate_llm_score": llm["score"],
                "gate_llm_details": llm,
                "gate_novelty_score": 0.0,
                "gate_novelty_details": None,
                "all_gates_passed": False,
            }

        novelty = check_novelty_gate(title, summary)
        all_passed = keyword["passed"] and llm["passed"] and novelty["passed"]

        logger.info(
            "Gates eval: keyword=%s, llm=%.3f, novelty=%.3f, all=%s",
            keyword["passed"], llm["score"],
            novelty["score"], all_passed,
        )

        return {
            "gate_keyword": keyword["passed"],
            "gate_keyword_details": keyword,
            "gate_llm_score": llm["score"],
            "gate_llm_details": llm,
            "gate_novelty_score": novelty["score"],
            "gate_novelty_details": novelty,
            "all_gates_passed": all_passed,
        }
    else:
        # Convention 2: combine pre-computed gate results
        kw_passed = kw_result.get("passed", False) if kw_result else False
        llm_passed = llm_gate.get("passed", False) if llm_gate else False
        nov_passed = nov_gate.get("passed", False) if nov_gate else False

        return {
            "keyword": {"passed": kw_passed, "result": kw_result},
            "llm": {"passed": llm_passed, "result": llm_gate},
            "novelty": {"passed": nov_passed, "result": nov_gate},
            "all_passed": kw_passed and llm_passed and nov_passed,
        }


# ---------------------------------------------------------------------------
# Stateless gate helpers (used by scanner.py)
# ---------------------------------------------------------------------------

def gate_keyword(title: str, summary: str, keywords: list[str]) -> dict:
    """Simple keyword gate: check if title/summary contain any keywords.

    Args:
        title: Sanitized title.
        summary: Sanitized summary.
        keywords: List of keyword strings.

    Returns:
        {"passed": bool, "matches": int, "min_required": int, "matched": list[str]}
    """
    config = get_intel_feed_config()
    min_matches = config.get("gates", {}).get("keyword_relevance", {}).get("min_keyword_matches", 1)

    text = f"{title} {summary}".lower()
    matched = [kw for kw in keywords if kw.lower() in text]

    return {
        "passed": len(matched) >= min_matches,
        "matches": len(matched),
        "min_required": min_matches,
        "matched": matched,
    }


def gate_llm_relevance(score: float, confidence: float, warmup_mode: bool = False) -> dict:
    """LLM relevance gate: check score against threshold.

    Args:
        score: LLM relevance score (0-1).
        confidence: LLM confidence score.
        warmup_mode: If True, use elevated warmup threshold.

    Returns:
        {"passed": bool}
    """
    config = get_intel_feed_config()
    threshold = config.get("gates", {}).get("llm_relevance", {}).get("threshold", 0.65)

    if warmup_mode:
        threshold = config.get("cold_start", {}).get("warmup_surface_threshold", 0.80)

    return {"passed": score >= threshold}


def gate_novelty(novelty_score: float) -> dict:
    """Novelty gate: check score against threshold.

    Args:
        novelty_score: Novelty score (0-1, higher = more novel).

    Returns:
        {"passed": bool}
    """
    config = get_intel_feed_config()
    threshold = config.get("gates", {}).get("novelty", {}).get("threshold", 0.30)

    return {"passed": novelty_score >= threshold}
