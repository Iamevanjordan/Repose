"""
Session_handoffTelegramHandoffWorkflow — triggered by Telegram ingest.
1. Receives session handoff dict from operator's Telegram message
2. Pulls Koda git activity (last 24h commits from /opt/agent-os)
3. Merges into clean business-state record
4. Writes to ORCA business-state + session-handoffs namespaces
5. Sends Axis primer back via Telegram
"""
from __future__ import annotations
import json
import logging
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

logger = logging.getLogger(__name__)


# ── Activities ────────────────────────────────────────────────────────────────

@activity.defn
async def pull_koda_git_activity() -> list[dict]:
    """Read git commits from /opt/agent-os in the last 24h. Never raises."""
    try:
        result = subprocess.run(
            ["git", "log", "--since=24 hours ago", "--format=%H|%s|%ai"],
            cwd="/opt/agent-os",
            capture_output=True,
            text=True,
            timeout=10,
        )
        commits = []
        for line in result.stdout.strip().splitlines():
            if "|" not in line:
                continue
            parts = line.split("|", 2)
            if len(parts) == 3:
                commits.append({
                    "hash": parts[0][:8],
                    "message": parts[1],
                    "timestamp": parts[2],
                })
        logger.info("Git activity: %d commits in last 24h", len(commits))
        return commits
    except Exception as e:
        logger.warning("Git activity pull failed (non-fatal): %s", e)
        return []


@activity.defn
async def write_telegram_handoff(
    handoff_data: dict, git_commits: list[dict], idempotency_key: str = ""
) -> dict:
    """
    Write the handoff to ORCA business-state and session-handoffs.
    Returns {business_state_id, handoff_id}.
    """
    import sys; sys.path.insert(0, "/opt/agent-os")
    from src.chronogram.client import get_client

    client = get_client()
    now = datetime.now(timezone.utc).isoformat()
    session_content = handoff_data.get("raw_content") or json.dumps(handoff_data, indent=2)

    # Build git summary
    git_lines = ""
    if git_commits:
        git_lines = "\n\nKODA ACTIVITY (last 24h):\n" + "\n".join(
            f"- [{c['hash']}] {c['message']}" for c in git_commits[:10]
        )

    merged_state = (
        f"SESSION HANDOFF — {now}\n\n"
        f"{session_content}"
        f"{git_lines}"
    )

    # RPOSE-005: each write gets a distinct deterministic idempotency_key so
    # ORCA can dedup them on Temporal retry.
    # Write to business-state
    biz_result = client.remember(
        namespace="business-state",
        content=merged_state,
        source="session_handoff-telegram",
        tags=["business-state", "session-handoff", now[:10], "namespace:business-state"],
        idempotency_key=f"{idempotency_key}-0" if idempotency_key else "",
    )
    biz_id = biz_result.get("memoryId", "unknown")

    # Write to session-handoffs
    handoff_result = client.remember(
        namespace="session-handoffs",
        content=json.dumps({
            "received_at": now,
            "content": session_content,
            "git_commits": git_commits[:10],
            "source": "telegram",
        }),
        source="session_handoff-telegram",
        tags=["session-handoff", now[:10], "namespace:session-handoffs"],
        idempotency_key=f"{idempotency_key}-1" if idempotency_key else "",
    )
    handoff_id = handoff_result.get("memoryId", "unknown")

    logger.info("Handoff written: biz=%s handoff=%s", biz_id, handoff_id)
    return {"business_state_id": biz_id, "handoff_id": handoff_id}


@activity.defn
async def send_handoff_primer(
    handoff_data: dict,
    git_commits: list[dict],
    record_ids: dict,
    idempotency_key: str = "",
) -> str:
    """Send the Axis primer back to the operator via Telegram."""
    import sys; sys.path.insert(0, "/opt/agent-os")
    import yaml
    from src.utils.telegram import send_message

    config_path = Path("/opt/agent-os/config/agents.yaml")
    cfg = yaml.safe_load(config_path.read_text())
    session_handoff_cfg = cfg.get("agents", {}).get("session_handoff", {})
    chat_id = str(session_handoff_cfg.get("telegram_chat_id", ""))
    secret_id = session_handoff_cfg.get("telegram_secret_id", "")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    session_content = handoff_data.get("raw_content") or json.dumps(handoff_data)

    # Git summary for primer
    git_lines = ""
    if git_commits:
        git_lines = "\n\nKoda commits (24h):\n" + "\n".join(
            f"• [{c['hash']}] {c['message']}" for c in git_commits[:5]
        )

    biz_id = record_ids.get("business_state_id", "?")

    primer = (
        f"SESSION_HANDOFF // {now}\n\n"
        f"Session handoff received + written to ORCA.\n"
        f"Record: {biz_id}\n"
        f"{git_lines}\n\n"
        f"---\n"
        f"AXIS SESSION PRIMER\n"
        f"Copy from here and paste as your first message to Axis.\n"
        f"---\n"
        f"{session_content[:800]}"
    )

    # RPOSE-005: deterministic idempotency_key so the Telegram router can dedup
    # this primer send on retry.
    receipt = await send_message(
        text=primer,
        chat_id=chat_id,
        telegram_secret_id=secret_id,
        parse_mode="",
        idempotency_key=idempotency_key,
    )
    msg_id = receipt.get("receipts", [{}])[0].get("message_id", "?")
    logger.info("Primer sent: message_id=%s", msg_id)
    return f"sent:message_id={msg_id}"


# ── Workflow ──────────────────────────────────────────────────────────────────

@workflow.defn(name="session_handoff-telegram-handoff")
class Session_handoffTelegramHandoffWorkflow:
    """Triggered when the operator sends SESSION_HANDOFF_HANDOFF to the Telegram bot."""

    @workflow.run
    async def run(self, handoff_data: dict) -> dict:
        retry = RetryPolicy(maximum_attempts=2, initial_interval=timedelta(seconds=5))
        to30 = timedelta(seconds=30)

        # RPOSE-005: deterministic idempotency key base from workflow id + run id.
        info = workflow.info()
        base = f"{info.workflow_id}-{info.run_id}"

        # Pull git activity
        git_commits: list[dict] = await workflow.execute_activity(
            pull_koda_git_activity,
            start_to_close_timeout=to30,
            retry_policy=RetryPolicy(maximum_attempts=1),
        )

        # Write to ORCA
        record_ids: dict = await workflow.execute_activity(
            write_telegram_handoff,
            args=[handoff_data, git_commits, f"{base}-write_telegram_handoff"],
            start_to_close_timeout=to30,
            retry_policy=retry,
        )

        # Send primer
        primer_result: str = await workflow.execute_activity(
            send_handoff_primer,
            args=[handoff_data, git_commits, record_ids,
                  f"{base}-send_handoff_primer-0"],
            start_to_close_timeout=to30,
            retry_policy=retry,
        )

        return {
            "business_state_id": record_ids.get("business_state_id"),
            "handoff_id": record_ids.get("handoff_id"),
            "git_commits": len(git_commits),
            "primer": primer_result,
        }
