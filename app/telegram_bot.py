"""
telegram_bot.py
Control Tower V3 — full rebuild.

New in V3:
- Phase commands: phases, where are we, what's left, approved
- Conversation state (yes/approved know what they're responding to)
- GPT-4o intent parsing for unrecognised messages (no dead-end help menu)
- Proactive stall detection pings
- HOLD escalation follow-up (once, after 4 hours)
- Builder PR open notifications
- New notifier methods for phase events
"""

import logging
from datetime import datetime, timezone, timedelta
from telegram import Bot, Update
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext
from telegram.parsemode import ParseMode

from app.readiness_gate import build_pr_readiness_block

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# NOTIFIER — outbound messages to Cheuck
# ─────────────────────────────────────────────

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

        trigger_note = ""
        if hold_trigger == "cost":
            trigger_note = "\n💰 *Cost impact detected — your call needed*"
        elif hold_trigger == "client_output":
            trigger_note = "\n📊 *Client output affected — your call needed*"
        elif hold_trigger == "product_quality":
            trigger_note = "\n⭐ *Product quality impact — your call needed*"
        elif hold_trigger == "live_system":
            trigger_note = "\n🔴 *Live system change — your call needed*"

        msg = (
            f"{decision_emoji} *PR #{pr_number} — {decision}*\n\n"
            f"*{pr_title}*\n\n"
            f"*Summary:* {summary}\n\n"
            f"*Reasoning:* {reasoning}\n\n"
            f"*Risks:*\n{risks_text}"
            f"{trigger_note}"
        )
        msg += build_pr_readiness_block(
            pr_title=pr_title,
            decision=decision,
            summary=summary,
            reasoning=reasoning,
            risks=risks,
            human_reason=human_reason,
            hold_trigger=hold_trigger,
            pr_number=pr_number,
        )

        if human_reason:
            msg += (
                f"\n\n⚠️ *Action needed:* {human_reason}"
                f"\n\nReply with:\n"
                f"`approve {pr_number}` — merge\n"
                f"`reject {pr_number}` — close\n"
                f"`details {pr_number}` — show files\n"
                f"`skip {pr_number}` — ignore"
            )
        else:
            msg += f"\n\n[View PR]({pr_url})"

        self.send_message(msg)

    def send_auto_merge_notification(self, pr_number, pr_title, next_step=""):
        msg = f"✅ *Auto-merged PR #{pr_number}*\n\n_{pr_title}_\n\nAll checks passed. Low risk. Railway deploying."
        if next_step:
            msg += f"\n\n{next_step}"
        self.send_message(msg)

    def send_builder_pr_opened(self, pr_number, pr_title, pr_url, issue_number):
        """Notify Cheuck when the AI builder opens a new PR."""
        msg = (
            f"🤖 *Builder opened PR #{pr_number}*\n\n"
            f"_{pr_title}_\n\n"
            f"Picking it up for review now. You'll only hear from me if there's a HOLD.\n\n"
            f"[View PR]({pr_url})"
        )
        self.send_message(msg)

    def send_ci_failure_alert(self, pr_number, pr_title, pr_url, failed_checks):
        checks_text = "\n".join([f"• {c}" for c in failed_checks])
        msg = f"🚨 *CI Failed — PR #{pr_number}*\n\n*{pr_title}*\n\n*Failed:*\n{checks_text}\n\n[View PR]({pr_url})"
        self.send_message(msg)

    def send_merge_success(self, pr_number, pr_title):
        self.send_message(f"🚀 *Merged*\n\nPR #{pr_number}: {pr_title}\n\nRailway deploy triggered.")

    def send_stall_alert(self, days_since: float, last_merge_at: str):
        msg = (
            f"⚠️ *Build stall detected*\n\n"
            f"No PRs merged in {days_since} days.\n"
            f"Last merge: {last_merge_at}\n\n"
            f"Builder may be stuck. Send `where are we` to check status."
        )
        self.send_message(msg)

    def send_hold_followup(self, pr_number, pr_title):
        """Follow up once on an unanswered HOLD alert."""
        msg = (
            f"⏰ *Following up on PR #{pr_number}*\n\n"
            f"_{pr_title}_\n\n"
            f"This was flagged for your review 4 hours ago. Still waiting.\n\n"
            f"`approve {pr_number}` — merge\n"
            f"`reject {pr_number}` — close\n"
            f"`skip {pr_number}` — ignore for now"
        )
        self.send_message(msg)

    def send_phase_complete_alert(self, phase_id: int, phase_name: str, summary: str):
        msg = (
            f"🏁 *Phase {phase_id} looks complete*\n\n"
            f"*{phase_name}*\n\n"
            f"{summary}\n\n"
            f"Reply `approved` to move to the next phase."
        )
        self.send_message(msg)

    def send_phase_advanced(self, completed_id: int, next_id: int, next_name: str):
        msg = (
            f"✅ *Phase {completed_id} signed off*\n\n"
            f"Starting Phase {next_id}: {next_name}\n\n"
            f"Send `what's next` to get the first task briefing."
        )
        self.send_message(msg)

    def send_weekly_summary(self, summary: str):
        msg = f"📋 *Weekly Summary*\n\n{summary}"
        self.send_message(msg)

    def send_startup_message(self):
        self.send_message(
            "⬡ *Control Tower V3 Online*\n\n"
            "Watching `cheuckolate-sketch/creator-campaign-os-backend`\n\n"
            "Safe PRs auto-merge. I'll only ping you for cost, client output, or quality decisions.\n\n"
            "Commands: `what's next` · `phases` · `where are we` · `what's left` · `weekly`"
        )

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


# ─────────────────────────────────────────────
# COMMAND HANDLER — inbound messages from Cheuck
# ─────────────────────────────────────────────

class TelegramCommandHandler:
    def __init__(self, token: str, chat_id: str, action_callback, project_manager=None):
        self.token = token
        self.chat_id = chat_id
        self.action_callback = action_callback
        self.project_manager = project_manager
        self.updater = Updater(token=token)
        self.dispatcher = self.updater.dispatcher

        # Conversation state — tracks what Tower last said so "yes" and "approved" route correctly
        self.last_action = None        # what Tower last did
        self.last_context = ""         # what Tower last said (for GPT-4o intent parsing)
        self.pending_phase_id = None   # phase id waiting for "approved"

        self._setup_handlers()

    def _setup_handlers(self):
        self.dispatcher.add_handler(
            MessageHandler(Filters.text & ~Filters.command, self._handle_message)
        )
        self.dispatcher.add_handler(CommandHandler("status", self._handle_status))
        self.dispatcher.add_handler(CommandHandler("help", self._handle_help))

    def set_last_context(self, action: str, message: str):
        """Called by main.py after Tower sends a message, so bot knows conversation state."""
        self.last_action = action
        self.last_context = message[:300]

    def set_pending_phase(self, phase_id: int):
        """Called by main.py when Tower detects a phase is complete and is waiting for approval."""
        self.pending_phase_id = phase_id

    def _handle_message(self, update: Update, context: CallbackContext):
        if str(update.message.chat_id) != str(self.chat_id):
            return

        text = update.message.text.strip()
        text_lower = text.lower()
        parts = text_lower.split()

        # ── PR COMMANDS: approve/reject/details/skip <number> ──
        if len(parts) == 2 and parts[0] in ["approve", "reject", "details", "skip"]:
            try:
                pr_number = int(parts[1])
                update.message.reply_text(f"Got it. Processing: {parts[0]} PR #{pr_number}...")
                self.action_callback(parts[0], pr_number, update)
                return
            except ValueError:
                update.message.reply_text("Invalid PR number. Try: approve 14")
                return

        # ── APPROVED — phase sign-off (must check BEFORE yes/kickoff block) ──
        if text_lower in ["approved", "approve phase", "phase approved", "sign off", "sign it off"]:
            if self.pending_phase_id:
                update.message.reply_text(f"Signing off Phase {self.pending_phase_id}...")
                self.action_callback("approve_phase", self.pending_phase_id, update)
                self.pending_phase_id = None
            else:
                update.message.reply_text(
                    "No phase waiting for approval right now. "
                    "Send `phases` to see current status."
                )
            return

        # ── PHASE COMMANDS ──
        if any(phrase in text_lower for phrase in ["phases", "all phases", "phase status", "show phases"]):
            update.message.reply_text("Pulling phase map...")
            self.action_callback("phases", None, update)
            return

        if any(phrase in text_lower for phrase in ["where are we", "where we at", "where we are", "current phase", "overall progress"]):
            update.message.reply_text("Checking where we are...")
            self.action_callback("where_are_we", None, update)
            return

        if any(phrase in text_lower for phrase in ["what's left", "whats left", "what left", "still missing", "remaining", "what's missing", "whats missing"]):
            update.message.reply_text("Running gap analysis...")
            self.action_callback("whats_left", None, update)
            return

        # ── WHAT'S NEXT ──
        if any(phrase in text_lower for phrase in ["what's next", "whats next", "what next", "next step", "next"]):
            update.message.reply_text("Checking project status...")
            self.action_callback("whats_next", None, update)
            return

        # ── KICKOFF — yes/go/ok etc ──
        KICKOFF_PHRASES = [
            "yes", "yeah", "yep", "yup", "ok", "okay", "ok la", "ok lah",
            "go", "go ahead", "go lah", "go la", "kick it off", "start",
            "proceed", "sure", "sure la", "do it", "lets do it", "let's do it",
            "confirm", "confirmed", "jalan", "boleh", "can", "can lah", "let's go",
        ]
        is_short_kickoff = len(parts) <= 4 and text_lower in KICKOFF_PHRASES
        if is_short_kickoff:
            update.message.reply_text("On it. Creating the next task...")
            self.action_callback("kickoff", None, update)
            return

        # ── WEEKLY SUMMARY ──
        if any(phrase in text_lower for phrase in ["weekly", "week", "summary", "weekly summary", "monday"]):
            update.message.reply_text("Generating weekly summary...")
            self.action_callback("weekly_summary", None, update)
            return

        # ── STATUS ──
        if text_lower in ["status", "tower status", "health"]:
            self.action_callback("status", None, update)
            return

        # ── GPT-4o INTENT PARSING — for anything unrecognised ──
        if self.project_manager:
            update.message.reply_text("Let me think about that...")
            try:
                action = self.project_manager.parse_intent(text, self.last_context)
                if action != "unknown":
                    self.action_callback(action, None, update)
                    return
            except Exception as e:
                logger.error(f"Intent parsing failed: {e}")

        # ── FALLBACK ──
        update.message.reply_text(
            "*Commands:*\n\n"
            "*Project:*\n"
            "`what's next` — status and next step\n"
            "`yes` — kick off next task\n"
            "`phases` — all phases and status\n"
            "`where are we` — current phase detail\n"
            "`what's left` — gap analysis\n"
            "`approved` — sign off completed phase\n"
            "`weekly` — weekly summary\n\n"
            "*PRs:*\n"
            "`approve <#>` — merge\n"
            "`reject <#>` — close\n"
            "`details <#>` — show files\n"
            "`skip <#>` — ignore\n\n"
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
            "*Control Tower V3 Commands*\n\n"
            "*Project intelligence:*\n"
            "`what's next` — where the project stands and what's next\n"
            "`yes` — kick off the next milestone\n"
            "`phases` — all phases with status\n"
            "`where are we` — current phase detail and progress\n"
            "`what's left` — gap analysis for active phase\n"
            "`approved` — sign off a completed phase\n"
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
