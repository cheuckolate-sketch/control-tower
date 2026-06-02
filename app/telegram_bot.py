"""
telegram_bot.py
Handles outbound notifications to Cheuck and inbound approval responses.
Two-way: send alerts, receive approve/reject/details commands.
"""

import logging
import asyncio
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str = None):
        self.token = token
        self.chat_id = chat_id
        self.bot = Bot(token=token)
        self.pending_approvals = {}  # pr_number -> callback

    async def send_message(self, text: str):
        """Send a plain message to Cheuck."""
        if not self.chat_id:
            logger.warning("No chat_id set. Cannot send message.")
            return
        try:
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error(f"Telegram send failed: {e}")

    async def send_pr_alert(self, pr_number: int, pr_title: str, pr_url: str,
                             decision: str, summary: str, reasoning: str,
                             risks: list, human_reason: str):
        """Send a formatted PR review alert."""

        decision_emoji = {"MERGE": "✅", "FIX": "🔧", "HOLD": "⛔"}.get(decision, "❓")

        risks_text = "\n".join([f"• {r}" for r in risks]) if risks else "None identified"

        msg = f"""
{decision_emoji} *PR #{pr_number} — {decision}*

*{pr_title}*

*Summary:* {summary}

*Reasoning:* {reasoning}

*Risks:*
{risks_text}
"""

        if human_reason:
            msg += f"\n⚠️ *Your call needed:* {human_reason}"
            msg += f"\n\nReply with:\n`approve {pr_number}` — merge it\n`reject {pr_number}` — close without merge\n`details {pr_number}` — show full diff\n`skip {pr_number}` — ignore for now"
        else:
            msg += f"\n[View PR]({pr_url})"

        await self.send_message(msg)

    async def send_ci_failure_alert(self, pr_number: int, pr_title: str,
                                     pr_url: str, failed_checks: list):
        """Alert when CI checks fail."""
        checks_text = "\n".join([f"• {c}" for c in failed_checks])
        msg = f"""
🚨 *CI Failed — PR #{pr_number}*

*{pr_title}*

*Failed checks:*
{checks_text}

[View PR]({pr_url})

Reply with:
`skip {pr_number}` — ignore for now
`details {pr_number}` — show what failed
"""
        await self.send_message(msg)

    async def send_merge_success(self, pr_number: int, pr_title: str):
        msg = f"🚀 *Merged & Deploying*\n\nPR #{pr_number}: {pr_title}\n\nRailway deploy triggered automatically."
        await self.send_message(msg)

    async def send_startup_message(self):
        msg = "⬡ *Control Tower Online*\n\nWatching `cheuckolate-sketch/creator-campaign-os-backend`\n\nI'll ping you when something needs your call."
        await self.send_message(msg)

    async def get_chat_id(self):
        """Print the chat ID of whoever messaged the bot last — for first-run setup."""
        try:
            updates = await self.bot.get_updates()
            if updates:
                chat_id = updates[-1].message.chat_id
                logger.info(f"Your Telegram chat ID: {chat_id}")
                print(f"\n✅ YOUR TELEGRAM CHAT ID: {chat_id}")
                print(f"Add this to your .env file as TELEGRAM_CHAT_ID={chat_id}\n")
                return str(chat_id)
            else:
                print("\n⚠️  No messages found. Send any message to your bot first, then restart.\n")
                return None
        except Exception as e:
            logger.error(f"Failed to get chat ID: {e}")
            return None


class TelegramCommandHandler:
    """Handles inbound commands from Cheuck via Telegram."""

    def __init__(self, token: str, chat_id: str, action_callback):
        self.token = token
        self.chat_id = chat_id
        self.action_callback = action_callback  # function(action, pr_number) -> None
        self.app = Application.builder().token(token).build()
        self._setup_handlers()

    def _setup_handlers(self):
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message))
        self.app.add_handler(CommandHandler("status", self._handle_status))
        self.app.add_handler(CommandHandler("help", self._handle_help))

    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Parse approve/reject/details/skip commands."""
        if str(update.message.chat_id) != str(self.chat_id):
            return  # ignore messages from anyone else

        text = update.message.text.strip().lower()
        parts = text.split()

        if len(parts) == 2 and parts[0] in ["approve", "reject", "details", "skip"]:
            action = parts[0]
            try:
                pr_number = int(parts[1])
                await update.message.reply_text(f"Got it. Processing: {action} PR #{pr_number}...")
                await self.action_callback(action, pr_number, update)
            except ValueError:
                await update.message.reply_text("Invalid PR number. Try: `approve 14`")
        else:
            await update.message.reply_text(
                "Commands:\n`approve <PR#>` — merge\n`reject <PR#>` — close\n`details <PR#>` — show diff\n`skip <PR#>` — ignore\n`/status` — tower status"
            )

    async def _handle_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if str(update.message.chat_id) != str(self.chat_id):
            return
        await self.action_callback("status", None, update)

    async def _handle_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if str(update.message.chat_id) != str(self.chat_id):
            return
        msg = """
*Control Tower Commands*

`approve <PR#>` — Merge PR into main
`reject <PR#>` — Close PR without merging
`details <PR#>` — Show PR files + CI status
`skip <PR#>` — Ignore PR for now
`/status` — Show tower health
`/help` — This message
"""
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

    async def start(self):
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()
        logger.info("Telegram command handler started.")

    async def stop(self):
        await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()
