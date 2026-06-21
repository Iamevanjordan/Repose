"""Session_handoff session-wrap Temporal workflow."""
from __future__ import annotations
import json
from datetime import timedelta
from pathlib import Path
import glob

from temporalio import activity, workflow
from temporalio.common import RetryPolicy


@activity.defn
async def read_and_validate_handoff(handoff_path: str) -> dict:
    import sys; sys.path.insert(0, "/opt/agent-os")
    from src.agents.session_handoff.schema import HandoffRecord
    path = Path(handoff_path)
    if not path.exists():
        raise FileNotFoundError(f"Handoff file not found: {handoff_path}")
    raw = json.loads(path.read_text())
    record = HandoffRecord.model_validate(raw)
    return record.model_dump()


@activity.defn
async def write_handoff_to_chronogram(record_dict: dict, idempotency_key: str = "") -> str:
    import sys; sys.path.insert(0, "/opt/agent-os")
    from src.chronogram.client import get_client
    client = get_client()
    # RPOSE-005: deterministic idempotency_key lets ORCA dedup this
    # non-idempotent write on Temporal retry.
    result = client.remember(
        namespace="session-handoffs",
        content=json.dumps(record_dict, indent=2),
        source="session_handoff-workflow",
        tags=["handoff", record_dict.get("milestone", "unknown"), record_dict.get("session_date", "")],
        agent_name="session_handoff",
        idempotency_key=idempotency_key,
    )
    return result["memoryId"]


@activity.defn
async def write_business_state_delta(record_dict: dict, idempotency_key: str = "") -> str:
    import sys; sys.path.insert(0, "/opt/agent-os")
    from src.chronogram.client import get_client
    client = get_client()
    state = record_dict.get("current_state", {})
    blockers = record_dict.get("blockers", [])
    debt = record_dict.get("known_debt", [])
    parts = [
        f"Session: {record_dict.get('session_date')} — {record_dict.get('milestone')}",
        f"Next step: {record_dict.get('next_step')}",
    ]
    if state.get("notes"):
        parts.append(f"State: {state['notes']}")
    if blockers:
        parts.append("Blockers: " + "; ".join(b["item"] for b in blockers))
    if debt:
        parts.append("Debt: " + "; ".join(debt))
    # RPOSE-005: deterministic idempotency_key for ORCA dedup on retry.
    result = client.remember(
        namespace="business-state",
        content="\n".join(parts),
        source="session_handoff-workflow",
        tags=["business-state", record_dict.get("milestone", "unknown")],
        agent_name="session_handoff",
        idempotency_key=idempotency_key,
    )
    return result["memoryId"]


@activity.defn
async def send_telegram_notification(record_dict: dict, idempotency_key: str = "") -> str:
    import sys; sys.path.insert(0, "/opt/agent-os")
    import yaml
    import httpx
    from src.secrets.bitwarden_client import get_secret

    config = yaml.safe_load(Path("/opt/agent-os/config/agents.yaml").read_text())
    session_handoff_cfg = config.get("agents", {}).get("session_handoff", {})

    secret_id = session_handoff_cfg.get("telegram_secret_id")
    chat_id = str(session_handoff_cfg.get("telegram_chat_id", ""))

    if not secret_id or not chat_id:
        return "skipped:no_telegram_config"

    token = get_secret(secret_id)

    async def _send(text: str, step_index: int) -> str:
        # RPOSE-005: deterministic per-message idempotency key as a custom header
        # so a Telegram-fronting router can dedup this send on Temporal retry.
        headers = {}
        if idempotency_key:
            headers["Idempotency-Key"] = f"{idempotency_key}-{step_index}"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                headers=headers,
            )
            if resp.status_code == 200:
                return str(resp.json().get("result", {}).get("message_id", "?"))
            return f"http_{resp.status_code}"

    milestone = record_dict.get("milestone", "?")
    next_step = record_dict.get("next_step", "?")
    session_date = record_dict.get("session_date", "?")

    # Part 1: session-wrap confirmation
    await _send(f"[Session_handoff] M{milestone} session wrapped. Next: {next_step}", 0)

    # Part 2: Axis session primer
    steps = record_dict.get("steps_completed", [])
    completed = "\n".join(
        f"• {s['step']}" for s in steps if s.get("pol", "").lower() == "pass"
    ) or "• None"

    blockers_list = record_dict.get("blockers", [])
    blockers_text = "\n".join(f"• {b['item']}" for b in blockers_list) or "• None"

    files_text = "\n".join(
        f"• {f}" for f in record_dict.get("files_modified", [])[:5]
    ) or "• None"

    security = "; ".join(record_dict.get("security_flags", [])) or "None"
    debt = "; ".join(record_dict.get("known_debt", [])) or "None"
    state_notes = record_dict.get("current_state", {}).get("notes", "No state notes")

    primer = (
        f"---\n"
        f"AXIS SESSION PRIMER\n"
        f"Copy from here and paste as your first message to Axis.\n"
        f"---\n"
        f"Session: M{milestone} — {session_date}\n"
        f"State: {state_notes}\n\n"
        f"Completed this session:\n{completed}\n\n"
        f"Active blockers:\n{blockers_text}\n\n"
        f"Next priority: {next_step}\n\n"
        f"Key files modified:\n{files_text}\n\n"
        f"Security flags: {security}\n"
        f"Known debt: {debt}\n"
        f"---"
    )

    msg_id = await _send(primer, 1)
    return f"sent:wrap+primer:msg_id={msg_id}"


@activity.defn
async def cleanup_handoff_files(handoff_path: str) -> int:
    files = glob.glob("/tmp/session_handoff_handoff_*.md")
    for f in files:
        Path(f).unlink(missing_ok=True)
    return len(files)


@workflow.defn
class Session_handoffSessionWrapWorkflow:
    @workflow.run
    async def run(self, handoff_path: str) -> dict:
        retry = RetryPolicy(maximum_attempts=2, initial_interval=timedelta(seconds=3))
        to = timedelta(seconds=30)

        # RPOSE-005: derive a deterministic idempotency key base from the
        # workflow id + run id so every retry of an activity reuses the same key.
        info = workflow.info()
        base = f"{info.workflow_id}-{info.run_id}"

        record = await workflow.execute_activity(
            read_and_validate_handoff, handoff_path,
            start_to_close_timeout=to, retry_policy=retry,
        )
        handoff_id = await workflow.execute_activity(
            write_handoff_to_chronogram,
            args=[record, f"{base}-write_handoff_to_chronogram-0"],
            start_to_close_timeout=to, retry_policy=retry,
        )
        biz_id = await workflow.execute_activity(
            write_business_state_delta,
            args=[record, f"{base}-write_business_state_delta-0"],
            start_to_close_timeout=to, retry_policy=retry,
        )
        tg_result = await workflow.execute_activity(
            send_telegram_notification,
            args=[record, f"{base}-send_telegram_notification"],
            start_to_close_timeout=to, retry_policy=retry,
        )
        deleted = await workflow.execute_activity(
            cleanup_handoff_files, handoff_path,
            start_to_close_timeout=to, retry_policy=retry,
        )

        return {
            "handoff_memory_id": handoff_id,
            "business_state_memory_id": biz_id,
            "telegram": tg_result,
            "files_deleted": deleted,
        }
