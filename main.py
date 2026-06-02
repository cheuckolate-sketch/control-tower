"""
main.py
Control Tower V2 — orchestrator.
V2 additions:
- Auto-merge safe PRs (no human needed)
- HOLD triggers: cost, client output, product quality, live system
- "what's next?" command with plain English project status
- "yes" command to kick off next milestone
- Weekly Monday summary
- Project memory via planner.py
"""

import logging
import os
import time
from datetime import datetime
from dotenv import load_dotenv

from app.github_client import GitHubClient
from app.reviewer import AIReviewer
from app.telegram_bot import TelegramNotifier, TelegramCommandHandler
from app.state import StateTracker
from app.planner import ProjectPlanner

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


class ControlTower:
    def __init__(self):
        self.github = GitHubClient(GITHUB_TOKEN, GITHUB_REPO)
        self.reviewer = AIReviewer(OPENAI_API_KEY, OPENAI_MODEL)
        self.reviewer.daily_limit = DAILY_LIMIT
        self.notifier = TelegramNotifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        self.state = StateTracker()
        self.planner = ProjectPlanner(OPENAI_API_KEY, OPENAI_MODEL)
        self.pending_kickoff = False  # waiting for Cheuck to say "yes"
        self.pending_issue_brief = None  # cached issue brief ready to create
        self.cmd_handler = TelegramCommandHandler(
            TELEGRAM_BOT_TOKEN,
            TELEGRAM_CHAT_ID,
            self.handle_telegram_action
        )

    def handle_telegram_action(self, action: str, pr_number, update):

        # ── STATUS ──
        if action == "status":
            stats = self.state.get_status()
            msg = (
                f"⬡ *Control Tower V2 Status*\n\n"
                f"PRs reviewed today: {stats['daily_stats']['prs_reviewed']}\n"
                f"PRs merged today: {stats['daily_stats']['prs_merged']}\n"
                f"PRs skipped: {stats['skipped_count']}\n"
                f"Repo: `{GITHUB_REPO}`\n\n"
                f"_Safe PRs auto-merge. You only get pinged for cost, client output, or quality decisions._"
            )
            update.message.reply_text(msg, parse_mode="Markdown")
            return

        # ── WHAT'S NEXT ──
        if action == "whats_next":
            try:
                open_issues = self.github.get_issues(state="open")
                open_prs = self.github.get_open_prs()
                response = self.planner.whats_next(open_issues, open_prs)
                self.pending_kickoff = True
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
                brief = self.planner.create_next_issue_brief()
                if not brief:
                    update.message.reply_text("Couldn't generate the issue brief. Try again.")
                    return

                # Create GitHub Issue
                issue = self.github.create_issue(
                    title=brief["title"],
                    body=brief["body"]
                )
                if issue:
                    self.pending_kickoff = False
                    msg = (
                        f"✅ *Task created — Issue #{issue.number}*\n\n"
                        f"_{brief['milestone']}_\n\n"
                        f"Now tell Codex this:\n\n"
                        f"`Work on GitHub Issue #{issue.number}. Open a PR. Do not merge. Use the issue as the full task brief.`\n\n"
                        f"Once Codex opens the PR, I'll take over from there."
                    )
                    update.message.reply_text(msg, parse_mode="Markdown")
                else:
                    update.message.reply_text("Failed to create GitHub Issue. Check GitHub directly.")
            except Exception as e:
                logger.error(f"Kickoff error: {e}")
                update.message.reply_text(f"Something went wrong: {str(e)[:200]}")
            return

        # ── WEEKLY SUMMARY ──
        if action == "weekly_summary":
            try:
                open_issues = self.github.get_issues(state="open")
                summary = self.planner.generate_weekly_summary([], open_issues)
                self.notifier.send_weekly_summary(summary)
            except Exception as e:
                logger.error(f"Weekly summary error: {e}")
                update.message.reply_text("Couldn't generate weekly summary right now.")
            return

        # ── PR ACTIONS ──
        pr = self.github.get_pr_by_number(pr_number)
        if not pr:
            update.message.reply_text(f"PR #{pr_number} not found.")
            return

        if action == "approve":
            success = self.github.merge_pr(pr)
            if success:
                self.state.mark_merged(pr_number)
                self.notifier.send_merge_success(pr_number, pr.title)
                update.message.reply_text(f"✅ PR #{pr_number} merged. Railway deploying.")
            else:
                update.message.reply_text(f"❌ Merge failed for PR #{pr_number}. Check GitHub.")

        elif action == "reject":
            pr.edit(state="closed")
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
            msg = f"*PR #{pr_number} Details*\n\n*Files:*\n{files_text or 'None'}\n\n*Checks:*\n{checks_text or 'None'}\n\n[View on GitHub]({pr.html_url})"
            update.message.reply_text(msg, parse_mode="Markdown")

        elif action == "skip":
            self.state.mark_skipped(pr_number)
            update.message.reply_text(f"⏭ PR #{pr_number} skipped.")

    def process_pr(self, pr):
        pr_number = pr.number

        if pr.draft:
            logger.info(f"PR #{pr_number} is draft. Skipping.")
            return

        if self.state.has_been_reviewed(pr_number):
            logger.debug(f"PR #{pr_number} already reviewed.")
            return

        if self.state.is_skipped(pr_number):
            logger.debug(f"PR #{pr_number} skipped by user.")
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
            logger.info(f"PR #{pr_number} has pending checks. Waiting.")
            return

        if failed_checks:
            self.notifier.send_ci_failure_alert(pr_number, pr.title, pr.html_url, failed_checks)
            self.state.mark_reviewed(pr_number, "CI_FAILED")
            return

        review = self.reviewer.review_pr(details)
        decision = review.get("decision", "HOLD")
        hold_trigger = review.get("hold_trigger", "none")

        # Build GitHub comment
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
*Reviewed at {datetime.now().strftime('%Y-%m-%d %H:%M UTC')} · Control Tower V2*
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
            logger.info(f"Auto-merging PR #{pr_number}")
            success = self.github.merge_pr(pr)
            if success:
                self.state.mark_merged(pr_number)
                self.notifier.send_auto_merge_notification(pr_number, pr.title)
                logger.info(f"PR #{pr_number} auto-merged successfully.")
            else:
                logger.error(f"Auto-merge failed for PR #{pr_number}. Notifying Cheuck.")
                self.notifier.send_message(
                    f"⚠️ Auto-merge failed for PR #{pr_number}: {pr.title}\n\nPlease check GitHub directly.\n`approve {pr_number}` to merge manually."
                )
            return

        # ── NOTIFY CHEUCK ──
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

        logger.info(f"PR #{pr_number} done. Decision: {decision}")

    def send_weekly_summary_if_monday(self):
        """Send weekly summary on Monday mornings."""
        now = datetime.now()
        if now.weekday() == 0 and now.hour == 9 and now.minute < 1:
            try:
                open_issues = self.github.get_issues(state="open")
                summary = self.planner.generate_weekly_summary([], open_issues)
                self.notifier.send_weekly_summary(summary)
                logger.info("Weekly summary sent.")
            except Exception as e:
                logger.error(f"Weekly summary error: {e}")

    def run(self):
        logger.info("Control Tower V2 starting...")

        if not TELEGRAM_CHAT_ID:
            chat_id = self.notifier.get_chat_id()
            if not chat_id:
                logger.error("No chat ID. Send a message to your bot and restart.")
                return
            self.notifier.chat_id = chat_id
            self.cmd_handler.chat_id = chat_id

        self.cmd_handler.start()
        self.notifier.send_startup_message()

        while True:
            try:
                prs = self.github.get_open_prs()
                logger.info(f"Found {len(prs)} open PR(s)")
                for pr in prs:
                    self.process_pr(pr)

                self.send_weekly_summary_if_monday()

            except Exception as e:
                logger.error(f"Poll error: {e}")
                self.notifier.send_message(f"⚠️ Control Tower error: {str(e)[:200]}")

            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    tower = ControlTower()
    tower.run()
