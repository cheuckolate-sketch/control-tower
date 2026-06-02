"""
telegram_bot.py
Handles outbound notifications and inbound commands.
V2: Added whats_next, yes/kick it off, weekly summary, auto-merge notifications.
Uses python-telegram-bot v13.x
"""

import logging
from telegram import Bot, Update
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext
from telegram.parsemode import ParseMode

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str = None):
        self.token = token
        self.chat_id = chat_id
        self.bot = Bot(token=token)

    def send_message(self, text: str):
        if not self.chat_id:
            logger.warning("No chat_id set. Cannot send message.")
            return
        try:
            self.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error(f"Telegram send failed: {e}")

    def send_pr_alert(self, pr_number, pr_title, pr_url,
                      decision, summary, reasoning, risks, human_reason, hold_trigger="none"):

        decision_emoji = {"MERGE": "✅", "FIX": "🔧", "HOLD": "⛔", "AUTO_MERGE": "✅"}.get(decision, "❓")
        risks_text = "\n".join([f"• {r}" for r in risks]) if risks else "None"

        # Add context for HOLD trigger type
        trigger_note = ""
        if hold_trigger == "cost":
            trigger_note = "\n💰 *Cost impact detected — your call needed*"
        elif hold_trigger == "client_output":
            trigger_note = "\n📊 *Client output affected — your call needed*"
        elif hold_trigger == "product_quality":
            trigger_note = "\n⭐ *Product quality impact — your call needed*"
        elif hold_trigger == "live_system":
            trigger_note = "\n🔴 *Live system change — your call needed*"

        msg = f"{decision_emoji} *PR #{pr_number} — {decision}*\n\n*{pr_title}*\n\n*Summary:* {summary}\n\n*Reasoning:* {reasoning}\n\n*Risks:*\n{risks_text}{trigger_note}"

        if human_reason:
            msg += f"\n\n⚠️ *Action needed:* {human_reason}"
            msg += f"\n\nReply with:\n`approve {pr_number}` — merge\n`reject {pr_number}` — close\n`details {pr_number}` — show files\n`skip {pr_number}` — ignore"
        else:
            msg += f"\n\n[View PR]({pr_url})"

        self.send_message(msg)

    def send_auto_merge_notification(self, pr_number, pr_title, next_step=""):
        msg = f"✅ *Auto-merged PR #{pr_number}*\n\n_{pr_title}_\n\nAll checks passed. Low risk. Railway deploying."
        if next_step:
            msg += f"\n\n{next_step}"
        self.send_message(msg)

    def send_ci_failure_alert(self, pr_number, pr_title, pr_url, failed_checks):
        checks_text = "\n".join([f"• {c}" for c in failed_checks])
        msg = f"🚨 *CI Failed — PR #{pr_number}*\n\n*{pr_title}*\n\n*Failed:*\n{checks_text}\n\n[View PR]({pr_url})"
        self.send_message(msg)

    def send_merge_success(self, pr_number, pr_title):
        self.send_message(f"🚀 *Merged*\n\nPR #{pr_number}: {pr_title}\n\nRailway deploy triggered.")

    def send_startup_message(self):
        self.send_message(
            "⬡ *Control Tower V2 Online*\n\n"
            "Watching `cheuckolate-sketch/creator-campaign-os-backend`\n\n"
            "Safe PRs will auto-merge. I'll only ping you for cost, client output, or quality decisions.\n\n"
            "Send `what's next` to get a project update."
        )

    def send_weekly_summary(self, summary: str):
        msg = f"📋 *Weekly Summary*\n\n{summary}"
        self.send_message(msg)

    def get_chat_id(self):
        try:
            updates = self.bot.get_updates()
            if updates:
                chat_id = updates[-1].message.chat_id
                print(f"\n✅ YOUR TELEGRAM CHAT ID: {chat_id}")
                print(f"Add to .env: TELEGRAM_CHAT_ID={chat_id}\n")
                return str(chat_id)
            else:
                print("\n⚠️  Send any message to your bot first, then restart.\n")
                return None
        except Exception as e:
            logger.error(f"Failed to get chat ID: {e}")
            return None


class TelegramCommandHandler:
    def __init__(self, token: str, chat_id: str, action_callback):
        self.token = token
        self.chat_id = chat_id
        self.action_callback = action_callback
        self.updater = Updater(token=token)
        self.dispatcher = self.updater.dispatcher
        self._setup_handlers()

    def _setup_handlers(self):
        self.dispatcher.add_handler(
            MessageHandler(Filters.text & ~Filters.command, self._handle_message)
        )
        self.dispatcher.add_handler(CommandHandler("status", self._handle_status))
        self.dispatcher.add_handler(CommandHandler("help", self._handle_help))

    def _handle_message(self, update: Update, context: CallbackContext):
        if str(update.message.chat_id) != str(self.chat_id):
            return

        text = update.message.text.strip().lower()
        parts = text.split()

        # PR commands: approve/reject/details/skip + number
        if len(parts) == 2 and parts[0] in ["approve", "reject", "details", "skip"]:
            action = parts[0]
            try:
                pr_number = int(parts[1])
                update.message.reply_text(f"Got it. Processing: {action} PR #{pr_number}...")
                self.action_callback(action, pr_number, update)
            except ValueError:
                update.message.reply_text("Invalid PR number. Try: approve 14")

        # Project intelligence commands
        elif any(phrase in text for phrase in ["what's next", "whats next", "what next", "next"]):
            update.message.reply_text("Checking project status...")
            self.action_callback("whats_next", None, update)

        elif text in ["yes", "yeah", "ok", "go", "go ahead", "kick it off", "start", "proceed"]:
            update.message.reply_text("On it. Creating the next task...")
            self.action_callback("kickoff", None, update)

        elif text in ["weekly", "week", "summary", "weekly summary"]:
            update.message.reply_text("Generating weekly summary...")
            self.action_callback("weekly_summary", None, update)

        else:
            update.message.reply_text(
                "Commands:\n\n"
                "*Project:*\n"
                "`what's next` — project status\n"
                "`yes` — kick off next task\n"
                "`weekly` — weekly summary\n\n"
                "*PRs:*\n"
                "`approve <PR#>` — merge\n"
                "`reject <PR#>` — close\n"
                "`details <PR#>` — show files\n"
                "`skip <PR#>` — ignore\n\n"
                "`/status` — tower health\n"
                "`/help` — this message",
                parse_mode="Markdown"
            )

    def _handle_status(self, update: Update, context: CallbackContext):
        if str(update.message.chat_id) != str(self.chat_id):
            return
        self.action_callback("status", None, update)

    def _handle_help(self, update: Update, context: CallbackContext):
        if str(update.message.chat_id) != str(self.chat_id):
            return
        update.message.reply_text(
            "*Control Tower V2 Commands*\n\n"
            "*Project intelligence:*\n"
            "`what's next` — where the project stands and what's next\n"
            "`yes` — kick off the next milestone\n"
            "`weekly` — Monday morning summary\n\n"
            "*PR management:*\n"
            "`approve <PR#>` — merge PR\n"
            "`reject <PR#>` — close PR\n"
            "`details <PR#>` — show files and checks\n"
            "`skip <PR#>` — stop alerting about this PR\n\n"
            "`/status` — tower health and daily stats\n"
            "`/help` — this message\n\n"
            "_Safe PRs auto-merge. You only get pinged for cost, client output, or quality decisions._",
            parse_mode="Markdown"
        )

    def start(self):
        self.updater.start_polling()
        logger.info("Telegram command handler started.")

    def stop(self):
        self.updater.stop()
