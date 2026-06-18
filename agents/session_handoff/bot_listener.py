"""
Session_handoff Telegram Bot Listener.
Persistent process that polls Telegram for inbound messages from the operator.
Triggers Session_handoffTelegramHandoffWorkflow on valid SESSION_HANDOFF_HANDOFF messages.

Start via systemd: repose-session_handoff-listener.service
"""
import asyncio
import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

sys.path.insert(0, "/opt/agent-os")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("session_handoff.bot_listener")


def _load_config() -> dict:
    return yaml.safe_load(Path("/opt/agent-os/config/agents.yaml").read_text())


def _get_bot_token() -> str:
    from src.secrets.bitwarden_client import get_secret
    cfg = _load_config()
    secret_id = cfg["agents"]["session_handoff"]["telegram_secret_id"]
    return get_secret(secret_id)


def _get_allowed_chat_id() -> int:
    cfg = _load_config()
    return int(cfg["agents"]["session_handoff"]["telegram_chat_id"])


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle all incoming messages — filter to SESSION_HANDOFF_HANDOFF only."""
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    chat_id = update.effective_chat.id
    allowed = context.bot_data.get("allowed_chat_id")

    # Only accept from the operator's personal chat_id
    if allowed and chat_id != allowed:
        logger.warning("Rejected message from unauthorized chat_id=%s", chat_id)
        return

    # Only process SESSION_HANDOFF_HANDOFF blocks
    if not (text.startswith("SESSION_HANDOFF_HANDOFF:") or text.startswith("SESSION_HANDOFF_HANDOFF:")):
        return

    logger.info("Received SESSION_HANDOFF_HANDOFF from chat_id=%s", chat_id)
    await update.message.reply_text("Received. Processing session handoff...")

    # Parse the handoff content
    content = text.replace("SESSION_HANDOFF_HANDOFF:", "", 1).strip()
    try:
        handoff_data = json.loads(content)
    except json.JSONDecodeError:
        handoff_data = {
            "raw_content": content,
            "received_at": datetime.now(timezone.utc).isoformat(),
            "format": "markdown",
        }

    # Trigger the Temporal workflow
    await _trigger_workflow(handoff_data, context)


async def _trigger_workflow(handoff_data: dict, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start Session_handoffTelegramHandoffWorkflow via Temporal."""
    try:
        from temporalio.client import Client
        from src.config import load_config

        cfg = load_config()
        temporal_addr = cfg.temporal.address
        # RPOSE-011: include a UUID4 component so two messages arriving in the
        # same second cannot collide on the same workflow id.
        wf_id = f"session_handoff-telegram-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"

        client = await Client.connect(temporal_addr)
        handle = await client.start_workflow(
            "session_handoff-telegram-handoff",
            handoff_data,
            id=wf_id,
            task_queue="agents",
        )
        logger.info("Session_handoffTelegramHandoffWorkflow started: %s", handle.id)
    except Exception as e:
        logger.error("Failed to trigger workflow: %s", e)
        # Notify operator about the failure
        allowed = context.bot_data.get("allowed_chat_id")
        if allowed:
            try:
                await context.bot.send_message(
                    chat_id=allowed,
                    text=f"Session_handoff workflow failed to start: {type(e).__name__}: {e}",
                )
            except Exception:
                pass


def main() -> None:
    logger.info("Loading config and secrets...")
    try:
        token = _get_bot_token()
        allowed_chat_id = _get_allowed_chat_id()
    except Exception as e:
        logger.error("Startup failed — could not load config/secrets: %s", e)
        sys.exit(1)

    logger.info("Bot token loaded (%d chars), allowed_chat_id=%s", len(token), allowed_chat_id)

    app = Application.builder().token(token).build()
    app.bot_data["allowed_chat_id"] = allowed_chat_id

    # Handle all text messages — filter logic is inside the handler
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Starting Session_handoff bot listener (polling)...")
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
