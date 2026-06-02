"""
main.py
Control Tower V3 — orchestrator.

V3 additions:
- ProjectManager replaces ProjectPlanner (unified PM intelligence)
- Phase commands: phases, where_are_we, whats_left, approve_phase
- Conversation state (yes/approved know what they're responding to)
- Stall detection in poll loop (pings Cheuck if no PRs in 48hrs)
- HOLD escalation follow-up (once, after 4 hours)
- Builder PR notifications (pings when ai-built PR opens)
- Weekly summary now fetches real merged PRs
"""

import logging
import os
import time
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

from app.github_client import GitHubClient
from app.reviewer import AIReviewer
from app.telegram_bot import TelegramNotifier, TelegramCommandHandler
from app.state import StateTracker
from app.project_manager import ProjectManager

load_dotenv()

# ── LOGGING ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("control_tower.log")
    ]
)
logger = logging.getLogger("ControlTower")

# ── CONFIG ──
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO", "cheuckolate-sketch/creator-campaign-os-backend")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
DAILY_LIMIT = int(os.getenv("DAILY_OPENAI_CALL_LIMIT", "50"))

BOT_MARKER = "<!-- control-tower-review -->"

# How long to wait before following up on an unanswered HOLD (seconds)
HOLD_FOLLOWUP_DELAY = 4 * 60 * 60  # 4 hours

# How long between stall checks (seconds) — only ping once per stall period
STALL_CHECK_INTERVAL = 24 * 60 * 60  # 24 hours


class ControlTower:
    def __init__(self):
        self.github = GitHubClient(GITHUB_TOKEN, GITHUB_REPO)
        self.reviewer = AIReviewer(OPENAI_API_KEY, OPENAI_MODEL)
        self.reviewer.daily_limit = DAILY_LIMIT
        self.notifier = TelegramNotifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        self.state = StateTracker()
        self.pm = ProjectManager(github_client=self.github)

        # Conversation state
        self.pending_kickoff = False       # waiting for Cheuck to say "yes" after whats_next
        self.pending_phase_id = None       # waiting for "approved" after phase complete alert

        # HOLD escalation tracking: {pr_number: datetime when HOLD was sent}
        self.hold_sent_at: dict[int, datetime] = {}
        self.hold_followed_up: set[int] = set()

        # Stall tracking
        self.last_stall_ping: datetime | None = None
        self.stall_detected: bool = False

        # Notified builder PRs (avoid double-pinging)
        self.notified_builder_prs: set[int] = set()

        self.cmd_handler = TelegramCommandHandler(
            TELEGRAM_BOT_TOKEN,
            TELEGRAM_CHAT_ID,
            self.handle_telegram_action,
            project_manager=self.pm,
        )

    # ─────────────────────────────────────────────
    # TELEGRAM ACTION HANDLER
    # ─────────────────────────────────────────────

    def handle_telegram_action(self, action: str, payload, update):

        # ── STATUS ──
        if action == "status":
            activity = self.github.get_recent_activity_summary()
            stats = self.state.get_status()
            msg = (
                f"⬡ *Control Tower V3 Status*\n\n"
                f"PRs reviewed today: {stats['daily_stats']['prs_reviewed']}\n"
                f"PRs merged today: {stats['daily_stats']['prs_merged']}\n"
                f"PRs skipped: {stats['skipped_count']}\n"
                f"Open PRs: {activity.get('open_prs', '?')}\n"
                f"Open issues: {activity.get('open_issues', '?')}\n"
                f"Last merge: {activity.get('last_merge_at', 'Unknown')}\n"
                f"Repo: `{GITHUB_REPO}`\n\n"
                f"_Safe PRs auto-merge. You only get pinged for cost, client output, or quality decisions._"
            )
            update.message.reply_text(msg, parse_mode="Markdown")
            return

        # ── WHAT'S NEXT ──
        if action == "whats_next":
            try:
                response = self.pm.get_full_briefing()
                self.pending_kickoff = True
                self.cmd_handler.set_last_context("whats_next", response)
                update.message.reply_text(response)
            except Exception as e:
                logger.error(f"whats_next error: {e}")
                update.message.reply_text("Having trouble checking status right now. Try again in a moment.")
            return

        # ── KICKOFF ──
        if action == "kickoff":
            if not self.pending_kickoff:
                update.message.reply_text("Send `what's next` first so I know what to kick off.")
                return
            try:
                update.message.reply_text("Creating the GitHub Issue and briefing the builder...")
                brief = self.pm.create_next_issue_brief()
                if not brief:
                    update.message.reply_text("Couldn't generate the issue brief. Try again.")
                    return

                issue = self.github.create_issue(
                    title=brief["title"],
                    body=brief["body"]
                )
                if issue:
                    self.pending_kickoff = False
                    msg = (
                        f"✅ *Issue #{issue.number} created*\n\n"
                        f"_{brief['milestone']}_\n\n"
                        f"Builder will pick this up automatically via GitHub Actions. "
                        f"I'll ping you when the PR is ready."
                    )
                    self.cmd_handler.set_last_context("kickoff", msg)
                    update.message.reply_text(msg, parse_mode="Markdown")
                else:
                    update.message.reply_text("Failed to create GitHub Issue. Check GitHub directly.")
            except Exception as e:
                logger.error(f"Kickoff error: {e}")
                update.message.reply_text(f"Something went wrong: {str(e)[:200]}")
            return

        # ── PHASES ──
        if action == "phases":
            try:
                summary = self.pm.get_phase_summary()
                self.cmd_handler.set_last_context("phases", summary)
                update.message.reply_text(summary, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Phases error: {e}")
                update.message.reply_text("Couldn't load phase map right now.")
            return

        # ── WHERE ARE WE ──
        if action == "where_are_we":
            try:
                detail = self.pm.get_active_phase_detail()
                self.cmd_handler.set_last_context("where_are_we", detail)
                update.message.reply_text(detail, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Where are we error: {e}")
                update.message.reply_text("Couldn't check current phase right now.")
            return

        # ── WHAT'S LEFT ──
        if action == "whats_left":
            try:
                gap = self.pm.get_whats_left()
                self.cmd_handler.set_last_context("whats_left", gap)
                update.message.reply_text(gap, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Whats left error: {e}")
                update.message.reply_text("Couldn't run gap analysis right now.")
            return

        # ── APPROVE PHASE ──
        if action == "approve_phase":
            phase_id = payload  # passed from cmd_handler
            if not phase_id:
                update.message.reply_text("Not sure which phase to approve. Send `phases` to check.")
                return
            try:
                msg = self.pm.approve_phase(phase_id)
                self.pending_phase_id = None
                self.cmd_handler.set_pending_phase(None)
                self.cmd_handler.set_last_context("approve_phase", msg)
                update.message.reply_text(msg, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Approve phase error: {e}")
                update.message.reply_text("Couldn't approve phase right now. Check GitHub directly.")
            return

        # ── WEEKLY SUMMARY ──
        if action == "weekly_summary":
            try:
                summary = self.pm.get_weekly_summary()
                self.notifier.send_weekly_summary(summary)
            except Exception as e:
                logger.error(f"Weekly summary error: {e}")
                update.message.reply_text("Couldn't generate weekly summary right now.")
            return

        # ── PR ACTIONS ──
        pr_number = payload
        pr = self.github.get_pr_by_number(pr_number)
        if not pr:
            update.message.reply_text(f"PR #{pr_number} not found.")
            return

        if action == "approve":
            success = self.github.merge_pr(pr)
            if success:
                self.state.mark_merged(pr_number)
                self.hold_sent_at.pop(pr_number, None)
                self.hold_followed_up.discard(pr_number)
                self.notifier.send_merge_success(pr_number, pr.title)
                update.message.reply_text(f"✅ PR #{pr_number} merged. Railway deploying.")
            else:
                update.message.reply_text(f"❌ Merge failed for PR #{pr_number}. Check GitHub.")

        elif action == "reject":
            pr.edit(state="closed")
            self.hold_sent_at.pop(pr_number, None)
            self.hold_followed_up.discard(pr_number)
            update.message.reply_text(f"🚫 PR #{pr_number} closed without merge.")

        elif action == "details":
            details = self.github.get_pr_details(pr)
            files = details.get("files_changed", [])
            checks = details.get("check_runs", [])
            files_text = "\n".join([
                f"• `{f['filename']}` ({f['status']}, +{f['additions']} -{f['deletions']})"
                for f in files[:15]
            ])
            checks_text = "\n".join([
                f"• {c['name']}: {c.get('conclusion') or c.get('status')}"
                for c in checks
            ])
            msg = (
                f"*PR #{pr_number} Details*\n\n"
                f"*Files:*\n{files_text or 'None'}\n\n"
                f"*Checks:*\n{checks_text or 'None'}\n\n"
                f"[View on GitHub]({pr.html_url})"
            )
            update.message.reply_text(msg, parse_mode="Markdown")

        elif action == "skip":
            self.state.mark_skipped(pr_number)
            self.hold_sent_at.pop(pr_number, None)
            update.message.reply_text(f"⏭ PR #{pr_number} skipped.")

    # ─────────────────────────────────────────────
    # PR PROCESSING
    # ─────────────────────────────────────────────

    def process_pr(self, pr):
        pr_number = pr.number

        if pr.draft:
            return

        if self.state.is_skipped(pr_number):
            return

        # Notify when builder PRs open (ai-built label)
        labels = [l.name for l in pr.labels]
        if "ai-built" in labels and pr_number not in self.notified_builder_prs:
            self.notifier.send_builder_pr_opened(pr_number, pr.title, pr.html_url, pr_number)
            self.notified_builder_prs.add(pr_number)

        if self.state.has_been_reviewed(pr_number):
            return

        logger.info(f"Processing PR #{pr_number}: {pr.title}")

        details = self.github.get_pr_details(pr)
        if not details:
            return

        failed_checks = [
            c["name"] for c in details.get("check_runs", [])
            if c.get("conclusion") in ["failure", "cancelled", "timed_out"]
        ]
        pending_checks = [
            c["name"] for c in details.get("check_runs", [])
            if c.get("status") in ["in_progress", "queued"]
        ]

        if pending_checks and not failed_checks:
            return

        if failed_checks:
            self.notifier.send_ci_failure_alert(pr_number, pr.title, pr.html_url, failed_checks)
            self.state.mark_reviewed(pr_number, "CI_FAILED")
            return

        review = self.reviewer.review_pr(details)
        decision = review.get("decision", "HOLD")
        hold_trigger = review.get("hold_trigger", "none")

        # Post GitHub comment
        decision_emoji = {"MERGE": "✅", "FIX": "🔧", "HOLD": "⛔", "AUTO_MERGE": "✅"}.get(decision, "❓")
        risks_md = "\n".join([f"- {r}" for r in review.get("risks", [])]) or "None"

        comment_body = f"""{BOT_MARKER}
## {decision_emoji} Control Tower Review — {decision}

**Summary:** {review.get('summary', '')}

**Reasoning:** {review.get('reasoning', '')}

**Risks:**
{risks_md}

**Confidence:** {review.get('confidence', 'unknown')}
**Hold Trigger:** {hold_trigger}

---
*Reviewed at {datetime.now().strftime('%Y-%m-%d %H:%M UTC')} · Control Tower V3*
"""
        if review.get("fix_instructions"):
            comment_body += f"\n**Fix Instructions:**\n{review['fix_instructions']}"

        existing = self.github.get_existing_bot_comment(pr, BOT_MARKER)
        if existing:
            self.github.update_pr_comment(existing, comment_body)
        else:
            self.github.post_pr_comment(pr, comment_body)

        self.state.mark_reviewed(pr_number, decision)

        # ── AUTO-MERGE ──
        if decision == "AUTO_MERGE" and review.get("confidence") == "high":
            success = self.github.merge_pr(pr)
            if success:
                self.state.mark_merged(pr_number)
                self.notifier.send_auto_merge_notification(pr_number, pr.title)
            else:
                self.notifier.send_message(
                    f"⚠️ Auto-merge failed for PR #{pr_number}: {pr.title}\n\n"
                    f"`approve {pr_number}` to merge manually."
                )
            return

        # ── HOLD — track for escalation follow-up ──
        if decision == "HOLD" and review.get("human_approval_required"):
            self.hold_sent_at[pr_number] = datetime.now(timezone.utc)

        self.notifier.send_pr_alert(
            pr_number=pr_number,
            pr_title=pr.title,
            pr_url=pr.html_url,
            decision=decision,
            summary=review.get("summary", ""),
            reasoning=review.get("reasoning", ""),
            risks=review.get("risks", []),
            human_reason=review.get("human_approval_reason", ""),
            hold_trigger=hold_trigger
        )

    # ─────────────────────────────────────────────
    # PROACTIVE CHECKS (run every poll cycle)
    # ─────────────────────────────────────────────

    def check_hold_escalations(self):
        """Follow up once on HOLD alerts that haven't been actioned in 4 hours."""
        now = datetime.now(timezone.utc)
        for pr_number, sent_at in list(self.hold_sent_at.items()):
            if pr_number in self.hold_followed_up:
                continue
            if (now - sent_at).total_seconds() >= HOLD_FOLLOWUP_DELAY:
                pr = self.github.get_pr_by_number(pr_number)
                if pr and pr.state == "open":
                    self.notifier.send_hold_followup(pr_number, pr.title)
                    self.hold_followed_up.add(pr_number)
                else:
                    # PR was closed or merged — clean up
                    self.hold_sent_at.pop(pr_number, None)

    def check_stall(self):
        """Ping Cheuck if no PRs merged in 48 hours. Only pings once per stall period."""
        now = datetime.now(timezone.utc)

        # Don't ping again within 24 hours of last stall ping
        if self.last_stall_ping and (now - self.last_stall_ping).total_seconds() < STALL_CHECK_INTERVAL:
            return

        stall_msg = self.pm.check_for_stall()
        if stall_msg:
            if not self.stall_detected:
                self.notifier.send_message(stall_msg)
                self.last_stall_ping = now
                self.stall_detected = True
        else:
            self.stall_detected = False

    def check_phase_completion(self):
        """Check if active phase looks done. Alert Cheuck and wait for 'approved'."""
        if self.pending_phase_id:
            return  # Already waiting for approval

        try:
            is_complete, active_phase, msg = self.pm.check_phase_completion()
            if is_complete and active_phase:
                self.pending_phase_id = active_phase["id"]
                self.cmd_handler.set_pending_phase(active_phase["id"])
                self.notifier.send_phase_complete_alert(
                    active_phase["id"],
                    active_phase["name"],
                    msg
                )
        except Exception as e:
            logger.error(f"Phase completion check error: {e}")

    def send_weekly_summary_if_monday(self):
        """Send weekly summary automatically on Monday at 9am."""
        now = datetime.now()
        if now.weekday() == 0 and now.hour == 9 and now.minute < 1:
            try:
                summary = self.pm.get_weekly_summary()
                self.notifier.send_weekly_summary(summary)
                logger.info("Weekly summary sent.")
            except Exception as e:
                logger.error(f"Weekly summary error: {e}")

    # ─────────────────────────────────────────────
    # MAIN LOOP
    # ─────────────────────────────────────────────

    def run(self):
        logger.info("Control Tower V3 starting...")

        if not TELEGRAM_CHAT_ID:
            chat_id = self.notifier.get_chat_id()
            if not chat_id:
                logger.error("No chat ID. Send a message to your bot and restart.")
                return
            self.notifier.chat_id = chat_id
            self.cmd_handler.chat_id = chat_id

        # Init phase map on startup
        try:
            self.pm.get_or_init_phases()
            logger.info("Phase map ready.")
        except Exception as e:
            logger.error(f"Phase map init failed: {e}")

        self.cmd_handler.start()
        self.notifier.send_startup_message()

        poll_count = 0

        while True:
            try:
                # ── PR REVIEW LOOP ──
                prs = self.github.get_open_prs()
                logger.info(f"Found {len(prs)} open PR(s)")
                for pr in prs:
                    self.process_pr(pr)

                # ── PROACTIVE CHECKS (every 5 poll cycles to reduce API calls) ──
                poll_count += 1
                if poll_count % 5 == 0:
                    self.check_hold_escalations()
                    self.check_stall()

                # ── PHASE COMPLETION CHECK (every 10 poll cycles) ──
                if poll_count % 10 == 0:
                    self.check_phase_completion()

                # ── WEEKLY SUMMARY ──
                self.send_weekly_summary_if_monday()

            except Exception as e:
                logger.error(f"Poll error: {e}")
                self.notifier.send_message(f"⚠️ Control Tower error: {str(e)[:200]}")

            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    tower = ControlTower()
    tower.run()
