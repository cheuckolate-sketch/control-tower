"""
telegram_bot.py
Handles outbound notifications and inbound approve/reject/details/skip commands.
Uses python-telegram-bot v13.x (stable on Python 3.13)
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
                      decision, summary, reasoning, risks, human_reason):
        decision_emoji = {"MERGE": "✅", "FIX": "🔧", "HOLD": "⛔"}.get(decision, "❓")
        risks_text = "\n".join([f"• {r}" for r in risks]) if risks else "None"

        msg = f"{decision_emoji} *PR #{pr_number} — {decision}*\n\n*{pr_title}*\n\n*Summary:* {summary}\n\n*Reasoning:* {reasoning}\n\n*Risks:*\n{risks_text}"

        if human_reason:
            msg += f"\n\n⚠️ *Your call needed:* {human_reason}"
            msg += f"\n\nReply with:\n`approve {pr_number}`\n`reject {pr_number}`\n`details {pr_number}`\n`skip {pr_number}`"
        else:
            msg += f"\n\n[View PR]({pr_url})"

        self.send_message(msg)

    def send_ci_failure_alert(self, pr_number, pr_title, pr_url, failed_checks):
        checks_text = "\n".join([f"• {c}" for c in failed_checks])
        msg = f"🚨 *CI Failed — PR #{pr_number}*\n\n*{pr_title}*\n\n*Failed:*\n{checks_text}\n\n[View PR]({pr_url})"
        self.send_message(msg)

    def send_merge_success(self, pr_number, pr_title):
        self.send_message(f"🚀 *Merged*\n\nPR #{pr_number}: {pr_title}\n\nRailway deploy triggered.")

    def send_startup_message(self):
        self.send_message("⬡ *Control Tower Online*\n\nWatching `cheuckolate-sketch/creator-campaign-os-backend`\n\nI'll ping you when something needs your call.")

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

        if len(parts) == 2 and parts[0] in ["approve", "reject", "details", "skip"]:
            action = parts[0]
            try:
                pr_number = int(parts[1])
                update.message.reply_text(f"Got it. Processing: {action} PR #{pr_number}...")
                self.action_callback(action, pr_number, update)
            except ValueError:
                update.message.reply_text("Invalid PR number. Try: approve 14")
        else:
            update.message.reply_text(
                "Commands:\napprove <PR#>\nreject <PR#>\ndetails <PR#>\nskip <PR#>\n/status\n/help"
            )

    def _handle_status(self, update: Update, context: CallbackContext):
        if str(update.message.chat_id) != str(self.chat_id):
            return
        self.action_callback("status", None, update)

    def _handle_help(self, update: Update, context: CallbackContext):
        if str(update.message.chat_id) != str(self.chat_id):
            return
        update.message.reply_text(
            "Control Tower Commands:\n\napprove <PR#> — Merge PR\nreject <PR#> — Close PR\ndetails <PR#> — Show files\nskip <PR#> — Ignore PR\n/status — Tower health\n/help — This message"
        )

    def start(self):
        self.updater.start_polling()
        logger.info("Telegram command handler started.")

    def stop(self):
        self.updater.stop()
