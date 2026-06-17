"""Morning_brief morning brief Temporal workflow."""
from __future__ import annotations
import dataclasses
import json
import logging
from datetime import timedelta
from pathlib import Path

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

logger = logging.getLogger(__name__)
CONFIG_PATH = "/opt/agent-os/config/morning_brief.yaml"


def _litellm_url() -> str:
    """LiteLLM chat-completions endpoint — from shared config or Bitwarden
    (RPOSE-008). No hardcoded host in src."""
    try:
        from repose.config import repose_config
        base = repose_config["infrastructure"]["litellm"]["url"].rstrip("/")
        return f"{base}/chat/completions"
    except Exception:
        from src.secrets.bitwarden_client import get_infra_secret
        return get_infra_secret("repose-litellm-url")


# ── Activities ────────────────────────────────────────────────────────────────

@activity.defn
async def harvest_context(config: dict) -> dict:
    import sys; sys.path.insert(0, "/opt/agent-os")
    import yaml
    from src.agents.morning_brief.context import build_context
    from src.chronogram.client import get_client
    from src.utils.tracer import activity_span

    with activity_span("harvest_context"):
        client = get_client()
        ctx = build_context(client, config)
        return dataclasses.asdict(ctx)


@activity.defn
async def compose_brief(ctx_dict: dict, config: dict) -> str:
    import sys; sys.path.insert(0, "/opt/agent-os")
    import httpx
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    from src.utils.tracer import activity_span

    _ET = ZoneInfo("America/New_York")
    brief_cfg = config.get("brief", {})
    model = brief_cfg.get("model", "claude-sonnet-4-6")
    max_tokens = brief_cfg.get("max_tokens", 1500)
    from src.secrets.bitwarden_client import get_infra_secret
    api_key = get_infra_secret("agent-os-production-agents-key")

    prompt_path = Path("/opt/agent-os") / brief_cfg.get("system_prompt_path", "src/agents/morning_brief/system_prompt.md")
    system_prompt = prompt_path.read_text()

    # Build context summary for LiteLLM
    sections = []
    if ctx_dict.get("business_focus"):
        items = ctx_dict["business_focus"][:5]
        sections.append("BUSINESS STATE:\n" + "\n".join(
            "- " + str(x.get("content") or "") for x in items
        ))
    if ctx_dict.get("system_events"):
        items = ctx_dict["system_events"][:3]
        sections.append("SYSTEM EVENTS (last 24h):\n" + "\n".join(
            "- " + str(x.get("content") or "") for x in items
        ))
    if ctx_dict.get("open_decisions"):
        items = ctx_dict["open_decisions"][:3]
        sections.append("OPEN DECISIONS:\n" + "\n".join(
            "- " + str(x.get("content") or "") for x in items
        ))
    if ctx_dict.get("build_errors"):
        sections.append("CONTEXT ERRORS:\n" + "\n".join(ctx_dict["build_errors"]))

    # Fallback: all namespace queries returned empty or errored — skip LiteLLM,
    # deliver a minimal brief so the schedule always fires.
    if not sections:
        run_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        error_note = ""
        if ctx_dict.get("build_errors"):
            error_note = f" ({len(ctx_dict['build_errors'])} namespace error(s))"
        return (
            f"Morning_brief brief — {run_ts}\n\n"
            f"System active. No new data since last run{error_note}. "
            f"Last known state: {run_ts}."
        )

    # Inject current Eastern Time date so LLM anchors to today, not stale context dates
    run_date = datetime.now(_ET).strftime("%A, %B %-d, %Y")
    user_content = f"TODAY'S DATE: {run_date}\n\n" + "\n\n".join(sections)

    with activity_span("compose_brief") as span:
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    _litellm_url(),
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_content},
                        ],
                        "max_tokens": max_tokens,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                brief_text = data["choices"][0]["message"]["content"]
                cost = data.get("usage", {}).get("prompt_tokens", 0)
                if span:
                    span.set_attribute("litellm_cost_usd", cost)
                return brief_text
        except Exception as e:
            logger.error("compose_brief LiteLLM call failed: %s", e)
            run_ts = datetime.now(_ET).strftime("%A, %B %-d, %Y")
            return (
                f"---\n"
                f"Morning Brief — {run_ts}\n\n"
                f"System active. Brief composition failed ({type(e).__name__}).\n"
                f"Worker logs have the full traceback. Shadow mode — investigate before May 17.\n"
                f"---"
            )


@activity.defn
async def deliver_telegram(brief_text: str, config: dict, idempotency_key: str = "") -> dict:
    import sys; sys.path.insert(0, "/opt/agent-os")
    from src.utils.telegram import send_message
    from src.utils.tracer import activity_span

    delivery_cfg = config.get("delivery", {})
    shadow_mode = delivery_cfg.get("shadow_mode", True)
    chat_id = str(delivery_cfg.get("debug_chat_id" if shadow_mode else "personal_chat_id", ""))
    secret_id = config.get("secrets", {}).get("telegram_token_secret_id", "")

    with activity_span("deliver_telegram"):
        # RPOSE-005: deterministic idempotency_key so the Telegram router can
        # dedup this send if the activity is retried.
        receipt = await send_message(
            text=brief_text,
            chat_id=chat_id,
            telegram_secret_id=secret_id,
            parse_mode="",
            idempotency_key=idempotency_key,
        )
        return receipt


@activity.defn
async def write_brief_to_chronogram(
    brief_text: str,
    ctx_dict: dict,
    receipt: dict,
    delivery_success: bool,
    config: dict,
    idempotency_key: str = "",
) -> str:
    import sys; sys.path.insert(0, "/opt/agent-os")
    import json
    from src.chronogram.client import get_client
    from src.utils.tracer import activity_span

    with activity_span("write_brief_to_chronogram"):
        client = get_client()
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        entry = {
            "date": today,
            "brief_text": brief_text[:2000],
            "delivery_receipt": receipt,
            "delivery_success": delivery_success,
            "build_errors": ctx_dict.get("build_errors", []),
            "context_summary": {
                "business_focus_count": len(ctx_dict.get("business_focus", [])),
                "system_events_count": len(ctx_dict.get("system_events", [])),
                "decisions_count": len(ctx_dict.get("open_decisions", [])),
            },
        }
        # RPOSE-005: deterministic idempotency_key for Chronogram dedup on retry.
        result = client.remember(
            namespace="morning_brief-briefs",
            content=json.dumps(entry),
            source="morning_brief-workflow",
            tags=["morning_brief-brief", today, "namespace:morning_brief-briefs"],
            agent_name="morning_brief",
            idempotency_key=idempotency_key,
        )
        return result["memoryId"]


# ── Workflow ──────────────────────────────────────────────────────────────────

@workflow.defn(name="morning_brief-morning-brief")
class Morning_briefBriefWorkflow:
    @workflow.run
    async def run(self, config: dict) -> dict:

        # RPOSE-005: deterministic idempotency key base from workflow id + run id
        # so each activity's writes/sends carry the same key across retries.
        info = workflow.info()
        idem_base = f"{info.workflow_id}-{info.run_id}"

        ctx_dict: dict = await workflow.execute_activity(
            harvest_context,
            args=[config],
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=30)),
        )

        brief_text: str = await workflow.execute_activity(
            compose_brief,
            args=[ctx_dict, config],
            start_to_close_timeout=timedelta(minutes=3),
            retry_policy=RetryPolicy(maximum_attempts=2, initial_interval=timedelta(seconds=15)),
        )

        delivery_success = True
        receipt: dict = {}
        try:
            receipt = await workflow.execute_activity(
                deliver_telegram,
                args=[brief_text, config, f"{idem_base}-deliver_telegram-0"],
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=10)),
            )
        except Exception as e:
            delivery_success = False
            logger.error("Morning_brief Telegram delivery failed: %s", e)

        await workflow.execute_activity(
            write_brief_to_chronogram,
            args=[brief_text, ctx_dict, receipt, delivery_success, config,
                  f"{idem_base}-write_brief_to_chronogram-0"],
            start_to_close_timeout=timedelta(minutes=1),
            retry_policy=RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=30)),
        )

        msg_id = None
        if receipt.get("receipts"):
            msg_id = receipt["receipts"][0].get("message_id")
        return {"delivered": delivery_success, "message_id": msg_id}
